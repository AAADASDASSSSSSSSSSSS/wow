package dev.ratsnest.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Dev-profile dispatch: launches the Python agent runtime as a local process.
 * The child gets RATSNEST_CONTROL_PLANE_URL so its Data Proxy streams ATDP
 * events back to this service while the run executes. In the docker/k8s
 * deployment the same contract flows over the network instead.
 */
@Service
public class RunDispatchService {

    private static final Logger log = LoggerFactory.getLogger(RunDispatchService.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final DesignRunRepository runs;

    @Value("${ratsnest.python-exe}")
    private String pythonExe;

    @Value("${ratsnest.agent-runtime-dir}")
    private String agentRuntimeDir;

    @Value("${ratsnest.self-url:http://localhost:8080}")
    private String selfUrl;

    public RunDispatchService(DesignRunRepository runs) {
        this.runs = runs;
    }

    @Async
    public void dispatch(String runId) {
        DesignRun run = runs.findById(runId).orElseThrow();
        try {
            List<String> cmd = List.of(
                    pythonExe, "-m", "ratsnest", "fix", run.getProjectDir(),
                    "--max-iter", String.valueOf(run.getMaxIterations()),
                    "--no-erc", "--json");
            ProcessBuilder pb = new ProcessBuilder(cmd)
                    .directory(new File(agentRuntimeDir))
                    .redirectErrorStream(false);
            pb.environment().put("RATSNEST_CONTROL_PLANE_URL", selfUrl);

            run.setStatus("running");
            runs.save(run);

            Process proc = pb.start();
            String stdout = new String(proc.getInputStream().readAllBytes(),
                    StandardCharsets.UTF_8);
            String stderr = new String(proc.getErrorStream().readAllBytes(),
                    StandardCharsets.UTF_8);
            boolean finished = proc.waitFor(15, TimeUnit.MINUTES);

            if (!finished || stdout.isBlank()) {
                run.setStatus("failed");
                log.error("run {} produced no output; stderr: {}", runId,
                        stderr.substring(0, Math.min(500, stderr.length())));
            } else {
                JsonNode record = MAPPER.readTree(stdout);
                run.setPythonRunId(record.path("run_id").asText(null));
                run.setStatus(record.path("status").asText("failed"));
                run.setStrategyVersionId(
                        record.path("strategy_version_id").asText(null));
                JsonNode iterations = record.path("iterations");
                if (iterations.isArray() && iterations.size() > 0) {
                    run.setFinalScore(iterations.get(iterations.size() - 1)
                            .path("scorecard").path("score").asDouble());
                    double delta0 = iterations.get(0).path("score_delta").asDouble(0);
                    double score0 = iterations.get(0).path("scorecard")
                            .path("score").asDouble();
                    run.setInitialScore(score0 - delta0);
                }
                run.setResultJson(stdout);
            }
        } catch (Exception e) {
            log.error("dispatch failed for run {}", runId, e);
            run.setStatus("failed");
        }
        run.setFinishedAt(Instant.now());
        runs.save(run);
    }
}
