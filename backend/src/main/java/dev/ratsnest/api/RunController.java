package dev.ratsnest.api;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.core.RunDispatchService;
import dev.ratsnest.security.RunAccessPolicy;
import org.springframework.core.io.InputStreamResource;
import org.springframework.core.io.Resource;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

@RestController
@RequestMapping("/api")
public class RunController {

    private static final Set<String> BACKENDS = Set.of("template", "crew", "mcp");

    public record CreateRunRequest(
            @jakarta.validation.constraints.NotBlank String projectDir,
            @jakarta.validation.constraints.Min(1)
            @jakarta.validation.constraints.Max(10) Integer maxIterations) {}

    public record CreateDesignRequest(
            @jakarta.validation.constraints.NotBlank
            @jakarta.validation.constraints.Size(max = 500) String requirement,
            @jakarta.validation.constraints.Min(1)
            @jakarta.validation.constraints.Max(10) Integer maxIterations,
            String backend) {}

    private final DesignRunRepository runs;
    private final RunDispatchService dispatch;
    private final RunAccessPolicy access;

    public RunController(DesignRunRepository runs, RunDispatchService dispatch,
                         RunAccessPolicy access) {
        this.runs = runs;
        this.dispatch = dispatch;
        this.access = access;
    }

    // -- create ---------------------------------------------------------------

    @PostMapping("/runs")
    public ResponseEntity<Map<String, String>> create(
            @jakarta.validation.Valid @RequestBody CreateRunRequest req) {
        int maxIter = req.maxIterations() == null ? 4 : req.maxIterations();
        DesignRun run = DesignRun.create(req.projectDir(), maxIter);
        run.setOwner(access.currentUser());
        runs.save(run);
        dispatch.dispatch(run.getId());
        return ResponseEntity.status(HttpStatus.ACCEPTED)
                .body(Map.of("runId", run.getId(), "status", run.getStatus()));
    }

    /** Design generation: requirement + backend in, verified board out. */
    @PostMapping("/designs")
    public ResponseEntity<Map<String, String>> createDesign(
            @jakarta.validation.Valid @RequestBody CreateDesignRequest req) {
        int maxIter = req.maxIterations() == null ? 4 : req.maxIterations();
        String backend = (req.backend() == null || req.backend().isBlank())
                ? "template" : req.backend().toLowerCase();
        if (!BACKENDS.contains(backend)) {
            throw new IllegalArgumentException(
                    "backend must be one of template, crew, mcp");
        }
        String projectDir = System.getProperty("java.io.tmpdir")
                + "/ratsnest-designs/" + UUID.randomUUID();
        DesignRun run = DesignRun.createDesign(
                req.requirement(), projectDir, maxIter, backend);
        run.setOwner(access.currentUser());
        runs.save(run);
        dispatch.dispatch(run.getId());
        return ResponseEntity.status(HttpStatus.ACCEPTED)
                .body(Map.of("runId", run.getId(), "status", run.getStatus(),
                        "backend", backend, "projectDir", projectDir));
    }

    /** Worker callback (kafka dispatch mode): RunRecord JSON in, row updated.
     *  Authenticated by the service token filter, not user JWT. */
    @PutMapping("/runs/{id}/result")
    public ResponseEntity<Map<String, String>> putResult(
            @PathVariable String id, @RequestBody String runRecordJson) {
        return runs.findById(id).map(run -> {
            try {
                dispatch.applyResult(run, runRecordJson);
            } catch (Exception e) {
                run.setStatus("failed");
            }
            run.setFinishedAt(java.time.Instant.now());
            runs.save(run);
            return ResponseEntity.ok(Map.of("status", run.getStatus()));
        }).orElse(ResponseEntity.notFound().build());
    }

    // -- read -----------------------------------------------------------------

    /** Bounded list, newest first. Non-admin users see only their own runs. */
    @GetMapping("/runs")
    public List<DesignRun> list(@RequestParam(defaultValue = "0") int page,
                                @RequestParam(defaultValue = "100") int size) {
        PageRequest pr = PageRequest.of(Math.max(0, page),
                Math.min(Math.max(1, size), 200),
                Sort.by(Sort.Direction.DESC, "createdAt"));
        String user = access.currentUser();
        if (user == null || access.currentIsAdmin()) {
            return runs.findAll(pr).getContent();       // open mode / admin
        }
        return runs.findByOwner(user, pr).getContent(); // scoped to owner
    }

    @GetMapping("/runs/{id}")
    public ResponseEntity<DesignRun> get(@PathVariable String id) {
        return runs.findById(id)
                .filter(access::canAccess)
                .map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }

    /** Download the full generated KiCad project (sch/pcb/pro + report +
     *  previews) as a zip. Local-dispatch: the project lives on this host.
     *  (Cluster/kafka mode moves this behind artifact storage — Phase 3.) */
    @GetMapping("/runs/{id}/download")
    public ResponseEntity<Resource> download(@PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess)
                .orElse(null);
        if (run == null || run.getProjectDir() == null) {
            return ResponseEntity.notFound().build();
        }
        Path dir = Path.of(run.getProjectDir());
        if (!Files.isDirectory(dir)) {
            return ResponseEntity.status(HttpStatus.GONE).build();
        }
        try {
            byte[] zip = zipDirectory(dir);
            String name = dir.getFileName() + ".zip";
            return ResponseEntity.ok()
                    .header(HttpHeaders.CONTENT_DISPOSITION,
                            "attachment; filename=\"" + name + "\"")
                    .contentType(MediaType.parseMediaType("application/zip"))
                    .body(new InputStreamResource(new ByteArrayInputStream(zip)));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
    }

    /** Read-only SVG preview: which = sch | pcb | step_NN_label (execution
     *  timeline frames emitted by the creator crew after each agent action). */
    @GetMapping("/runs/{id}/preview/{which}")
    public ResponseEntity<Resource> preview(@PathVariable String id,
                                            @PathVariable String which) {
        DesignRun run = runs.findById(id).filter(access::canAccess)
                .orElse(null);
        if (run == null || run.getProjectDir() == null
                || !which.matches("sch|pcb|step_[A-Za-z0-9_\\-]{1,60}")) {
            return ResponseEntity.notFound().build();
        }
        Path svg = which.startsWith("step_")
                ? Path.of(run.getProjectDir(), "preview", "steps", which + ".svg")
                : Path.of(run.getProjectDir(), "preview", which + ".svg");
        if (!Files.isRegularFile(svg)) {
            return ResponseEntity.notFound().build();
        }
        try {
            byte[] body = Files.readAllBytes(svg);
            return ResponseEntity.ok()
                    .contentType(MediaType.valueOf("image/svg+xml"))
                    .header(HttpHeaders.CACHE_CONTROL, "no-cache")
                    .body(new InputStreamResource(new ByteArrayInputStream(body)));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
    }

    /** Execution timeline frames available for a run, in step order. */
    @GetMapping("/runs/{id}/steps")
    public ResponseEntity<List<String>> steps(@PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess).orElse(null);
        if (run == null || run.getProjectDir() == null) {
            return ResponseEntity.notFound().build();
        }
        Path dir = Path.of(run.getProjectDir(), "preview", "steps");
        if (!Files.isDirectory(dir)) {
            return ResponseEntity.ok(List.of());
        }
        try (Stream<Path> files = Files.list(dir)) {
            return ResponseEntity.ok(files
                    .map(p -> p.getFileName().toString())
                    .filter(n -> n.endsWith(".svg"))
                    .map(n -> n.substring(0, n.length() - 4))
                    .sorted()
                    .toList());
        } catch (IOException e) {
            return ResponseEntity.ok(List.of());
        }
    }

    /** The kicad-happy style design report (markdown). */
    @GetMapping("/runs/{id}/report")
    public ResponseEntity<String> report(@PathVariable String id) {
        DesignRun run = runs.findById(id).filter(access::canAccess).orElse(null);
        if (run == null || run.getProjectDir() == null) {
            return ResponseEntity.notFound().build();
        }
        Path md = Path.of(run.getProjectDir(), "ratsnest_report.md");
        if (!Files.isRegularFile(md)) {
            return ResponseEntity.notFound().build();
        }
        try {
            return ResponseEntity.ok()
                    .contentType(MediaType.valueOf("text/markdown;charset=UTF-8"))
                    .body(Files.readString(md));
        } catch (IOException e) {
            return ResponseEntity.internalServerError().build();
        }
    }

    // -- helpers ----------------------------------------------------------------

    private static byte[] zipDirectory(Path dir) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        try (ZipOutputStream zos = new ZipOutputStream(buffer);
             Stream<Path> walk = Files.walk(dir)) {
            walk.filter(Files::isRegularFile).forEach(file -> {
                try {
                    zos.putNextEntry(new ZipEntry(dir.relativize(file).toString()
                            .replace('\\', '/')));
                    Files.copy(file, zos);
                    zos.closeEntry();
                } catch (IOException ignored) {
                    // skip unreadable/locked files
                }
            });
        }
        return buffer.toByteArray();
    }
}
