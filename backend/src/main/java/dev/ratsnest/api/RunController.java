package dev.ratsnest.api;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.core.RunDispatchService;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api")
public class RunController {

    public record CreateRunRequest(String projectDir, Integer maxIterations) {}
    public record CreateDesignRequest(String requirement, Integer maxIterations) {}

    private final DesignRunRepository runs;
    private final RunDispatchService dispatch;

    public RunController(DesignRunRepository runs, RunDispatchService dispatch) {
        this.runs = runs;
        this.dispatch = dispatch;
    }

    @PostMapping("/runs")
    public ResponseEntity<Map<String, String>> create(@RequestBody CreateRunRequest req) {
        if (req.projectDir() == null || req.projectDir().isBlank()) {
            return ResponseEntity.badRequest()
                    .body(Map.of("error", "projectDir is required"));
        }
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
            @RequestBody CreateDesignRequest req) {
        if (req.requirement() == null || req.requirement().isBlank()) {
            return ResponseEntity.badRequest()
                    .body(Map.of("error", "requirement is required"));
        }
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

    @GetMapping("/runs")
    public List<DesignRun> list() {
        return runs.findAll();
    }

    @GetMapping("/runs/{id}")
    public ResponseEntity<DesignRun> get(@PathVariable String id) {
        return runs.findById(id).map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }
}
