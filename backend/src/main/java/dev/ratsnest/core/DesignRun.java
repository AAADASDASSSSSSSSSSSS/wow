package dev.ratsnest.core;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;

import java.time.Instant;
import java.util.UUID;

/** Governance record of one design run. Payloads stay opaque JSON (typed by
 *  the shared contract schemas) — no domain logic in the control plane. */
@Entity
@Table(name = "design_runs")
public class DesignRun {

    @Id
    private String id;

    private String projectDir;
    private int maxIterations;
    private String status;          // dispatched|running|converged|escalated|failed
    private String pythonRunId;     // run_id assigned by the agent runtime
    private String strategyVersionId;
    private Double initialScore;
    private Double finalScore;
    private Instant createdAt;
    private Instant finishedAt;

    @Lob
    @Column(columnDefinition = "CLOB")
    private String resultJson;      // full RunRecord contract payload

    public static DesignRun create(String projectDir, int maxIterations) {
        DesignRun run = new DesignRun();
        run.id = UUID.randomUUID().toString();
        run.projectDir = projectDir;
        run.maxIterations = maxIterations;
        run.status = "dispatched";
        run.createdAt = Instant.now();
        return run;
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getProjectDir() { return projectDir; }
    public void setProjectDir(String projectDir) { this.projectDir = projectDir; }
    public int getMaxIterations() { return maxIterations; }
    public void setMaxIterations(int maxIterations) { this.maxIterations = maxIterations; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public String getPythonRunId() { return pythonRunId; }
    public void setPythonRunId(String pythonRunId) { this.pythonRunId = pythonRunId; }
    public String getStrategyVersionId() { return strategyVersionId; }
    public void setStrategyVersionId(String strategyVersionId) { this.strategyVersionId = strategyVersionId; }
    public Double getInitialScore() { return initialScore; }
    public void setInitialScore(Double initialScore) { this.initialScore = initialScore; }
    public Double getFinalScore() { return finalScore; }
    public void setFinalScore(Double finalScore) { this.finalScore = finalScore; }
    public Instant getCreatedAt() { return createdAt; }
    public void setCreatedAt(Instant createdAt) { this.createdAt = createdAt; }
    public Instant getFinishedAt() { return finishedAt; }
    public void setFinishedAt(Instant finishedAt) { this.finishedAt = finishedAt; }
    public String getResultJson() { return resultJson; }
    public void setResultJson(String resultJson) { this.resultJson = resultJson; }
}
