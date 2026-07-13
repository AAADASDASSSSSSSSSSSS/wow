package dev.ratsnest.core;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * The single owner of "invoke the Python agent runtime": builds
 * `<python-exe> -m ratsnest <args>` in the configured runtime directory.
 * Both the Web-EDA bridge and local run dispatch go through here.
 */
@Service
public class PythonBridge {

    @Value("${ratsnest.python-exe:python}")
    private String pythonExe;

    @Value("${ratsnest.agent-runtime-dir:.}")
    private String agentRuntimeDir;

    public record BridgeResult(boolean finished, String stdout, String stderr) {}

    public BridgeResult run(List<String> args, Duration timeout,
                            Map<String, String> extraEnv)
            throws IOException, InterruptedException {
        List<String> cmd = new ArrayList<>(List.of(pythonExe, "-m", "ratsnest"));
        cmd.addAll(args);
        ProcessBuilder pb = new ProcessBuilder(cmd)
                .directory(new File(agentRuntimeDir))
                .redirectErrorStream(false);
        pb.environment().putAll(extraEnv);
        Process proc = pb.start();
        // Bounded-output contract: the ratsnest CLI emits a small JSON result
        // on stdout and modest diagnostics on stderr, so draining stdout fully
        // then stderr cannot deadlock here. A future streaming/high-volume
        // caller would need a per-stream reader thread instead.
        String stdout = new String(proc.getInputStream().readAllBytes(),
                StandardCharsets.UTF_8);
        String stderr = new String(proc.getErrorStream().readAllBytes(),
                StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(timeout.toMillis(),
                TimeUnit.MILLISECONDS);
        return new BridgeResult(finished, stdout, stderr);
    }
}
