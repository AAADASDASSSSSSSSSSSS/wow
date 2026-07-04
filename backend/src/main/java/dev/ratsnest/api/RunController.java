package dev.ratsnest.api;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.core.RunDispatchService;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api")
public class RunController {

    public record CreateRunRequest(
            @jakarta.validation.constraints.NotBlank String projectDir,
            @jakarta.validation.constraints.Min(1)
            @jakarta.validation.constraints.Max(10) Integer maxIterations) {}
    public record CreateDesignRequest(
            @jakarta.validation.constraints.NotBlank
            @jakarta.validation.constraints.Size(max = 500) String requirement,
            @jakarta.validation.constraints.Min(1)
            @jakarta.validation.constraints.Max(10) Integer maxIterations) {}

    private final DesignRunRepository runs;
    private final RunDispatchService dispatch;

    public RunController(DesignRunRepository runs, RunDispatchService dispatch) {
        this.runs = runs;
        this.dispatch = dispatch;
    }

    @PostMapping("/runs")
    public ResponseEntity<Map<String, String>> create(
            @jakarta.validation.Valid @RequestBody CreateRunRequest req) {
        int maxIter = req.maxIterations() == null ? 4 : req.maxIterations();
        DesignRun run = DesignRun.create(req.projectDir(), maxIter);
        runs.save(run);
        dispatch.dispatch(run.getId());
        return ResponseEntity.status(HttpStatus.ACCEPTED)
                .body(Map.of("runId", run.getId(), "status", run.getStatus()));
    }

    /** Design generation: natural-language requirement in, verified board out. */
    @PostMapping("/designs")
    public ResponseEntity<Map<String, String>> createDesign(
            @jakarta.validation.Valid @RequestBody CreateDesignRequest req) {
        int maxIter = req.maxIterations() == null ? 4 : req.maxIterations();
        String projectDir = System.getProperty("java.io.tmpdir")
                + "/ratsnest-designs/" + java.util.UUID.randomUUID();
        DesignRun run = DesignRun.createDesign(req.requirement(), projectDir, maxIter);
        runs.save(run);
        dispatch.dispatch(run.getId());
        return ResponseEntity.status(HttpStatus.ACCEPTED)
                .body(Map.of("runId", run.getId(), "status", run.getStatus(),
                        "projectDir", projectDir));
    }

    /** Worker callback (kafka dispatch mode): RunRecord JSON in, row updated. */
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

    /** Bounded list, newest first (frontend-compatible array shape). */
    @GetMapping("/runs")
    public List<DesignRun> list(
            @org.springframework.web.bind.annotation.RequestParam(defaultValue = "0") int page,
            @org.springframework.web.bind.annotation.RequestParam(defaultValue = "100") int size) {
        return runs.findAll(org.springframework.data.domain.PageRequest.of(
                        Math.max(0, page), Math.min(Math.max(1, size), 200),
                        org.springframework.data.domain.Sort.by(
                                org.springframework.data.domain.Sort.Direction.DESC,
                                "createdAt")))
                .getContent();
    }

    @GetMapping("/runs/{id}")
    public ResponseEntity<DesignRun> get(@PathVariable String id) {
        return runs.findById(id).map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }
}
