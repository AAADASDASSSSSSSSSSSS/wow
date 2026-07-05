package dev.ratsnest.api;

import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Web-EDA bridge (Stage 3): the browser sends typed edit ops; the Python
 * runtime executes them through the trusted write paths and returns fresh
 * state. The browser never writes S-expressions. Local-dispatch mode only
 * (cluster mode needs artifact storage first — Phase 3 note applies).
 */
@RestController
@RequestMapping("/api")
public class EdaController {

    private final DesignRunRepository runs;

    @Value("${ratsnest.python-exe:python}")
    private String pythonExe;

    @Value("${ratsnest.agent-runtime-dir:.}")
    private String agentRuntimeDir;

    public EdaController(DesignRunRepository runs) {
        this.runs = runs;
    }

    @GetMapping("/runs/{id}/eda")
    public ResponseEntity<String> state(@PathVariable String id)
            throws Exception {
        return bridge(id, null);
    }

    @PostMapping("/runs/{id}/eda")
    public ResponseEntity<String> edit(@PathVariable String id,
                                       @RequestBody String opsJson)
            throws Exception {
        if (opsJson == null || opsJson.length() > 100_000) {
            return ResponseEntity.badRequest().build();
        }
        return bridge(id, opsJson);
    }

    private static boolean canTouch(DesignRun run) {
        var auth = org.springframework.security.core.context
                .SecurityContextHolder.getContext().getAuthentication();
        if (auth == null || !auth.isAuthenticated()) {
            return false;
        }
        boolean admin = auth.getAuthorities().stream().anyMatch(
                a -> a.getAuthority().contains("ADMIN")
                        || a.getAuthority().contains("SERVICE"));
        return admin || run.getOwner().equals(auth.getName());
    }

    private ResponseEntity<String> bridge(String id, String opsJson)
            throws Exception {
        DesignRun run = runs.findById(id).orElse(null);
        if (run == null || run.getProjectDir() == null
                || !Files.isDirectory(Path.of(run.getProjectDir()))) {
            return ResponseEntity.notFound().build();
        }
        if (run.getOwner() != null && !canTouch(run)) {
            return ResponseEntity.notFound().build();  // same policy as RunController
        }
        List<String> cmd = new java.util.ArrayList<>(List.of(
                pythonExe, "-m", "ratsnest", "eda", run.getProjectDir()));
        Path opsFile = null;
        if (opsJson != null) {
            opsFile = Files.createTempFile("ratsnest-eda", ".json");
            Files.writeString(opsFile, opsJson, StandardCharsets.UTF_8);
            cmd.add("--ops");
            cmd.add(opsFile.toString());
        }
        try {
            Process proc = new ProcessBuilder(cmd)
                    .directory(new File(agentRuntimeDir)).start();
            String stdout = new String(proc.getInputStream().readAllBytes(),
                    StandardCharsets.UTF_8);
            proc.waitFor(3, TimeUnit.MINUTES);
            if (stdout.isBlank()) {
                return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                        .body("{\"error\":\"eda bridge produced no output\"}");
            }
            return ResponseEntity.ok()
                    .contentType(MediaType.APPLICATION_JSON).body(stdout);
        } finally {
            if (opsFile != null) {
                Files.deleteIfExists(opsFile);
            }
        }
    }
}
