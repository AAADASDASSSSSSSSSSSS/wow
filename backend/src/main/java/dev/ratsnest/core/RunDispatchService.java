package dev.ratsnest.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Run dispatch — two modes, one contract:
 *   local  (default): spawn the Python agent runtime as a subprocess
 *   kafka  (cluster profile): publish a run request to `ratsnest.run-requests`;
 *          a Python worker consumes it and PUTs the RunRecord back to
 *          /api/runs/{id}/result.
 * Either way the child streams ATDP events to this service while it runs.
 */
@Service
public class RunDispatchService {

    private static final Logger log = LoggerFactory.getLogger(RunDispatchService.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final DesignRunRepository runs;
    private final ObjectProvider<KafkaTemplate<String, String>> kafka;
    private final PythonBridge bridge;

    @Value("${ratsnest.dispatch:local}")
    private String dispatchMode;

    @Value("${ratsnest.topic.run-requests:ratsnest.run-requests}")
    private String runRequestTopic;

    @Value("${ratsnest.self-url:http://localhost:8080}")
    private String selfUrl;

    @Value("${ratsnest.security.service-token:}")
    private String serviceToken;

    public RunDispatchService(DesignRunRepository runs,
                              ObjectProvider<KafkaTemplate<String, String>> kafka,
                              PythonBridge bridge) {
        this.runs = runs;
        this.kafka = kafka;
        this.bridge = bridge;
    }

    @Async
    public void dispatch(String runId) {
        DesignRun run = runs.findById(runId).orElseThrow();
        if ("kafka".equals(dispatchMode)) {
            dispatchKafka(run);
        } else {
            dispatchLocal(run);
        }
    }

    // -- kafka mode (cluster) -------------------------------------------------
    private void dispatchKafka(DesignRun run) {
        try {
            KafkaTemplate<String, String> template = kafka.getObject();
            ObjectNode msg = MAPPER.createObjectNode();
            msg.put("runId", run.getId());
            msg.put("kind", run.getKind());
            msg.put("requirement", run.getRequirement());
            msg.put("projectDir", run.getProjectDir());
            msg.put("maxIterations", run.getMaxIterations());
            msg.put("backend", run.getBackend());
            msg.put("callbackUrl", selfUrl + "/api/runs/" + run.getId() + "/result");
            msg.put("controlPlaneUrl", selfUrl);
            template.send(runRequestTopic, run.getId(), msg.toString());
            run.setStatus("queued");
        } catch (Exception e) {
            log.error("kafka dispatch failed for run {}", run.getId(), e);
            run.setStatus("failed");
            run.setFinishedAt(Instant.now());
        }
        runs.save(run);
    }

    // -- local mode (dev) -------------------------------------------------------
    private void dispatchLocal(DesignRun run) {
        try {
            List<String> cmd = new ArrayList<>();
            if ("design".equals(run.getKind())) {
                cmd.addAll(List.of("design", run.getRequirement(),
                        "--out", run.getProjectDir()));
                String backend = run.getBackend() == null ? "template"
                        : run.getBackend();
                cmd.addAll(List.of("--backend", backend));
            } else {
                cmd.addAll(List.of("fix", run.getProjectDir()));
            }
            cmd.addAll(List.of("--max-iter", String.valueOf(run.getMaxIterations()),
                    "--no-erc", "--json"));

            Map<String, String> env = new HashMap<>();
            env.put("RATSNEST_CONTROL_PLANE_URL", selfUrl);
            if (serviceToken != null && !serviceToken.isBlank()) {
                env.put("RATSNEST_SERVICE_TOKEN", serviceToken);
            }

            run.setStatus("running");
            runs.save(run);

            PythonBridge.BridgeResult result =
                    bridge.run(cmd, Duration.ofMinutes(15), env);

            if (!result.finished() || result.stdout().isBlank()) {
                run.setStatus("failed");
                log.error("run {} produced no output; stderr: {}", run.getId(),
                        result.stderr().substring(0,
                                Math.min(500, result.stderr().length())));
            } else {
                applyResult(run, result.stdout());
            }
        } catch (Exception e) {
            log.error("dispatch failed for run {}", run.getId(), e);
            run.setStatus("failed");
        }
        run.setFinishedAt(Instant.now());
        runs.save(run);
    }

    /** Parse a RunRecord contract payload into the governance row.
     *  Shared by local dispatch and the worker callback endpoint. */
    public void applyResult(DesignRun run, String runRecordJson) throws Exception {
        JsonNode record = MAPPER.readTree(runRecordJson);
        run.setPythonRunId(record.path("run_id").asText(null));
        run.setStatus(record.path("status").asText("failed"));
        run.setStrategyVersionId(record.path("strategy_version_id").asText(null));
        JsonNode iterations = record.path("iterations");
        if (iterations.isArray() && iterations.size() > 0) {
            run.setFinalScore(iterations.get(iterations.size() - 1)
                    .path("scorecard").path("score").asDouble());
            double delta0 = iterations.get(0).path("score_delta").asDouble(0);
            double score0 = iterations.get(0).path("scorecard")
                    .path("score").asDouble();
            run.setInitialScore(score0 - delta0);
        }
        run.setResultJson(runRecordJson);
    }
}
