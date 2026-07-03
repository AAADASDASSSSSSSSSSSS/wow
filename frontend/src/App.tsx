import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  motion,
  MotionValue,
  useInView,
  useScroll,
  useTransform
} from "framer-motion";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  CircuitBoard,
  Cpu,
  FlaskConical,
  GitBranch,
  RadioTower,
  Sparkles,
  Zap
} from "lucide-react";
import {
  createDesignRun,
  createRepairRun,
  getHealth,
  getRun,
  getRunEvents,
  listRuns
} from "./lib/api";
import {
  AtdpEvent,
  DesignRun,
  formatDate,
  formatScoreDelta,
  parseRunRecord,
  shortId,
  statusClassName,
  summarizeEvent
} from "./lib/runData";

const primaryText = "#E1E0CC";
const easeOut = [0.16, 1, 0.3, 1] as const;

interface WordsPullUpProps {
  text: string;
  className?: string;
  showAsterisk?: boolean;
  center?: boolean;
}

function WordsPullUp({
  text,
  className = "",
  showAsterisk = false,
  center = false
}: WordsPullUpProps) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const isInView = useInView(ref, { once: true, margin: "-10% 0px" });
  const words = text.split(" ");

  return (
    <span
      ref={ref}
      className={`inline-flex flex-wrap ${center ? "justify-center" : ""} ${className}`}
      aria-label={text}
    >
      {words.map((word, index) => (
        <span className="overflow-hidden pr-[0.08em]" key={`${word}-${index}`}>
          <motion.span
            aria-hidden="true"
            className="relative inline-block"
            initial={{ y: 28, opacity: 0 }}
            animate={isInView ? { y: 0, opacity: 1 } : { y: 28, opacity: 0 }}
            transition={{
              duration: 0.8,
              delay: index * 0.08,
              ease: easeOut
            }}
          >
            {word}
            {showAsterisk && index === words.length - 1 ? (
              <span className="absolute -right-[0.28em] top-[0.58em] text-[0.26em] leading-none">
                *
              </span>
            ) : null}
          </motion.span>
          {index < words.length - 1 ? <span aria-hidden="true">&nbsp;</span> : null}
        </span>
      ))}
    </span>
  );
}

interface Segment {
  text: string;
  className?: string;
}

function WordsPullUpMultiStyle({
  segments,
  className = ""
}: {
  segments: Segment[];
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const isInView = useInView(ref, { once: true, margin: "-10% 0px" });
  const words = segments.flatMap((segment, segmentIndex) =>
    segment.text.split(" ").map((word, wordIndex) => ({
      word,
      className: segment.className ?? "",
      key: `${segmentIndex}-${wordIndex}-${word}`
    }))
  );

  return (
    <div
      ref={ref}
      className={`inline-flex flex-wrap justify-center ${className}`}
    >
      {words.map((item, index) => (
        <span className="overflow-hidden pr-[0.12em]" key={item.key}>
          <motion.span
            className={`inline-block ${item.className}`}
            initial={{ y: 20, opacity: 0 }}
            animate={isInView ? { y: 0, opacity: 1 } : { y: 20, opacity: 0 }}
            transition={{
              duration: 0.7,
              delay: index * 0.055,
              ease: easeOut
            }}
          >
            {item.word}
          </motion.span>
          {index < words.length - 1 ? <span>&nbsp;</span> : null}
        </span>
      ))}
    </div>
  );
}

function AnimatedLetter({
  char,
  index,
  total,
  progress
}: {
  char: string;
  index: number;
  total: number;
  progress: MotionValue<number>;
}) {
  const start = Math.max(0, index / total - 0.1);
  const end = Math.min(1, index / total + 0.06);
  const opacity = useTransform(progress, [start, end], [0.22, 1]);

  return (
    <motion.span style={{ opacity }}>
      {char === " " ? "\u00A0" : char}
    </motion.span>
  );
}

function ScrollRevealText({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement | null>(null);
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start 0.85", "end 0.25"]
  });
  const chars = Array.from(text);

  return (
    <p
      ref={ref}
      className="mx-auto mt-8 max-w-3xl text-center text-xs leading-relaxed text-primary sm:text-sm md:text-base"
    >
      {chars.map((char, index) => (
        <AnimatedLetter
          char={char}
          index={index}
          key={`${char}-${index}`}
          progress={scrollYProgress}
          total={chars.length}
        />
      ))}
    </p>
  );
}

const features = [
  {
    icon: CircuitBoard,
    number: "01",
    title: "Design Generation.",
    copy: "Natural language becomes a KiCad project path with a verified run record.",
    checks: ["Template and MCP backends", "Typed design specification", "ERC-ready output trail"]
  },
  {
    icon: Zap,
    number: "02",
    title: "Repair Loop.",
    copy: "Findings become patch plans, score deltas, and converge-or-escalate decisions.",
    checks: ["Evaluate findings", "Apply repair mappings", "Reject new critical regressions"]
  },
  {
    icon: RadioTower,
    number: "03",
    title: "ATDP Trajectory.",
    copy: "Every orchestrator step and MCP tool call can be captured as learning signal.",
    checks: ["Node-level event stream", "Reward and outcome traces", "Control plane ingestion"]
  },
  {
    icon: GitBranch,
    number: "04",
    title: "Heuristic Evolution.",
    copy: "Candidate strategies are tested against benchmarks before they can replace incumbents.",
    checks: ["Candidate vs incumbent gates", "Rollback-safe promotion", "Benchmark-backed scoring"]
  }
];

function HealthPill({
  health,
  healthError
}: {
  health: string;
  healthError: string | null;
}) {
  const isOnline = health === "ok";

  return (
    <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-black/45 px-3 py-1.5 text-[11px] text-primary/75 backdrop-blur">
      <span
        className={`h-2 w-2 rounded-full ${isOnline ? "bg-emerald-300" : "bg-amber-300"}`}
      />
      <span>{isOnline ? "control plane online" : healthError ?? "checking API"}</span>
    </div>
  );
}

function Hero({
  health,
  healthError
}: {
  health: string;
  healthError: string | null;
}) {
  const navItems = ["System", "Agents", "Evolution", "Console", "Runs"];

  return (
    <section className="h-screen bg-black p-4 md:p-6">
      <div className="relative h-full overflow-hidden rounded-2xl bg-[#030303] md:rounded-[2rem]">
        <div className="lab-field absolute inset-0" />
        <div className="noise-overlay absolute inset-0 opacity-[0.72] mix-blend-overlay" />
        <div className="absolute inset-0 bg-gradient-to-b from-black/40 via-transparent to-black/75" />
        <div className="absolute left-1/2 top-0 z-20 -translate-x-1/2 rounded-b-2xl bg-black px-4 py-2 md:rounded-b-3xl md:px-8">
          <nav className="flex items-center gap-3 text-[10px] sm:gap-6 sm:text-xs md:gap-12 md:text-sm lg:gap-14">
            {navItems.map((item) => (
              <a
                href={item === "Console" || item === "Runs" ? "#console" : `#${item.toLowerCase()}`}
                key={item}
                style={{ color: "rgba(225, 224, 204, 0.8)" }}
                className="whitespace-nowrap transition-colors hover:text-[#E1E0CC]"
              >
                {item}
              </a>
            ))}
          </nav>
        </div>

        <div className="absolute left-4 top-16 z-10 sm:left-6 md:left-8">
          <HealthPill health={health} healthError={healthError} />
        </div>

        <div className="absolute bottom-0 left-0 right-0 z-10 px-4 pb-5 sm:px-6 md:px-8 md:pb-7">
          <div className="grid items-end gap-6 lg:grid-cols-12">
            <div className="lg:col-span-8">
              <h1
                className="text-[26vw] font-medium leading-[0.85] tracking-[-0.07em] sm:text-[24vw] md:text-[22vw] lg:text-[20vw] xl:text-[19vw] 2xl:text-[20vw]"
                style={{ color: primaryText }}
              >
                <WordsPullUp text="RatsNest" showAsterisk />
              </h1>
            </div>
            <div className="pb-2 lg:col-span-4 lg:pb-8">
              <motion.p
                initial={{ y: 20, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ delay: 0.5, duration: 0.8, ease: easeOut }}
                className="max-w-lg text-xs leading-[1.25] text-primary/70 sm:text-sm md:text-base"
              >
                Auto-evolving multi-agent control plane for KiCad design
                review, repair, and strategy evolution. It closes the loop
                from evaluation to repair to trajectory signal to better
                heuristics.
              </motion.p>
              <motion.a
                href="#console"
                initial={{ y: 20, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ delay: 0.7, duration: 0.8, ease: easeOut }}
                className="group mt-5 inline-flex items-center gap-2 rounded-full bg-primary px-4 py-2 text-sm font-bold text-black transition-all hover:gap-3 sm:text-base"
              >
                Launch a design run
                <span className="flex h-9 w-9 items-center justify-center rounded-full bg-black text-primary transition-transform group-hover:scale-110 sm:h-10 sm:w-10">
                  <ArrowRight size={18} />
                </span>
              </motion.a>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function SystemSection() {
  return (
    <section id="system" className="bg-black px-4 py-20 sm:px-6 md:py-28">
      <div className="mx-auto max-w-6xl rounded-[1.5rem] bg-[#101010] px-5 py-16 text-center sm:px-8 md:px-12 md:py-24">
        <p className="text-[10px] uppercase tracking-[0.35em] text-primary sm:text-xs">
          AHE control plane
        </p>
        <h2
          className="mx-auto mt-6 max-w-4xl text-3xl leading-[0.98] sm:text-4xl sm:leading-[0.94] md:text-5xl lg:text-6xl xl:text-7xl"
          style={{ color: primaryText }}
        >
          <WordsPullUpMultiStyle
            segments={[
              { text: "This is not a PCB generator." },
              {
                text: "It is a strategy evolution loop.",
                className: "font-serif italic"
              },
              { text: "Every design run becomes training signal." }
            ]}
          />
        </h2>
        <ScrollRevealText text="RatsNest separates governance from intelligence: Spring Boot stores runs and trajectories, Python agents evaluate and repair KiCad projects, ATDP records what happened, and AHE promotes only strategies that pass benchmark gates." />
      </div>
    </section>
  );
}

function FeaturesSection() {
  const ref = useRef<HTMLDivElement | null>(null);
  const isInView = useInView(ref, { once: true, margin: "-100px 0px" });

  return (
    <section
      id="agents"
      className="relative min-h-screen overflow-hidden bg-black px-4 py-20 sm:px-6 md:py-28"
    >
      <div className="bg-noise absolute inset-0 opacity-[0.15]" />
      <div className="relative mx-auto max-w-7xl">
        <div className="mx-auto max-w-4xl text-center">
          <WordsPullUpMultiStyle
            className="text-xl font-normal leading-tight text-primary sm:text-2xl md:text-3xl lg:text-4xl"
            segments={[
              { text: "Studio-grade autonomy for PCB repair loops." },
              {
                text: "Built for traceability. Powered by evolution.",
                className: "text-gray-500"
              }
            ]}
          />
        </div>

        <div
          ref={ref}
          className="mt-12 grid gap-3 sm:mt-16 md:grid-cols-2 md:gap-2 lg:h-[480px] lg:grid-cols-4 lg:gap-1"
        >
          <motion.div
            initial={{ scale: 0.95, opacity: 0 }}
            animate={isInView ? { scale: 1, opacity: 1 } : { scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.75, ease: [0.22, 1, 0.36, 1] }}
            className="relative min-h-[320px] overflow-hidden rounded-lg bg-[#141414] p-5 lg:h-full"
          >
            <div className="lab-card-bg absolute inset-0" />
            <div className="absolute inset-0 bg-gradient-to-b from-transparent via-black/20 to-black/70" />
            <div className="relative flex h-full flex-col justify-end">
              <p className="text-sm uppercase tracking-[0.3em] text-primary/55">
                live system
              </p>
              <h3 className="mt-3 text-3xl leading-none text-[#E1E0CC] md:text-4xl">
                From run data to better strategy.
              </h3>
            </div>
          </motion.div>

          {features.map((feature, index) => {
            const Icon = feature.icon;
            return (
              <motion.article
                className="flex min-h-[320px] flex-col rounded-lg bg-[#212121] p-5 text-primary lg:h-full"
                initial={{ scale: 0.95, opacity: 0 }}
                animate={
                  isInView ? { scale: 1, opacity: 1 } : { scale: 0.95, opacity: 0 }
                }
                transition={{
                  duration: 0.75,
                  delay: (index + 1) * 0.15,
                  ease: [0.22, 1, 0.36, 1]
                }}
                key={feature.title}
              >
                <div className="flex h-12 w-12 items-center justify-center rounded-md bg-black/45 text-primary">
                  <Icon size={22} />
                </div>
                <div className="mt-8 flex items-start justify-between gap-4">
                  <h3 className="text-xl leading-none text-[#E1E0CC]">
                    {feature.title}
                  </h3>
                  <span className="text-xs text-gray-500">{feature.number}</span>
                </div>
                <p className="mt-4 text-sm leading-relaxed text-gray-400">
                  {feature.copy}
                </p>
                <ul className="mt-6 space-y-3">
                  {feature.checks.map((check) => (
                    <li
                      className="flex items-start gap-2 text-sm text-gray-400"
                      key={check}
                    >
                      <Check className="mt-0.5 text-primary" size={15} />
                      <span>{check}</span>
                    </li>
                  ))}
                </ul>
                <a
                  className="mt-auto inline-flex items-center gap-2 pt-8 text-sm text-primary"
                  href="#console"
                >
                  Learn more
                  <ArrowRight className="-rotate-45" size={16} />
                </a>
              </motion.article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function ConsoleSection() {
  const [runs, setRuns] = useState<DesignRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<DesignRun | null>(null);
  const [events, setEvents] = useState<AtdpEvent[]>([]);
  const [requirement, setRequirement] = useState("");
  const [projectDir, setProjectDir] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmittingDesign, setIsSubmittingDesign] = useState(false);
  const [isSubmittingRepair, setIsSubmittingRepair] = useState(false);

  const sortedRuns = useMemo(
    () =>
      [...runs].sort((a, b) =>
        (b.createdAt ?? "").localeCompare(a.createdAt ?? "")
      ),
    [runs]
  );

  const selectedRecord = useMemo(
    () => parseRunRecord(selectedRun?.resultJson),
    [selectedRun?.resultJson]
  );

  const selectedIterations = selectedRecord?.iterations ?? [];

  const refreshRuns = useCallback(async () => {
    try {
      const nextRuns = await listRuns();
      setRuns(nextRuns);
      setError(null);
      setSelectedRunId((current) => {
        if (current || nextRuns.length === 0) {
          return current;
        }
        return nextRuns[0].id;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load runs");
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
    const timer = window.setInterval(() => {
      void refreshRuns();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [refreshRuns]);

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null);
      setEvents([]);
      return;
    }

    let active = true;
    async function loadSelected() {
      try {
        const [run, runEvents] = await Promise.all([
          getRun(selectedRunId as string),
          getRunEvents(selectedRunId as string).catch(() => [])
        ]);
        if (!active) {
          return;
        }
        setSelectedRun(run);
        setEvents(runEvents);
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Unable to load run");
        }
      }
    }

    void loadSelected();
    return () => {
      active = false;
    };
  }, [selectedRunId, runs]);

  async function submitDesign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = requirement.trim();
    if (!trimmed) {
      return;
    }

    setIsSubmittingDesign(true);
    try {
      const response = await createDesignRun(trimmed);
      setSelectedRunId(response.runId);
      setRequirement("");
      await refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Design run failed");
    } finally {
      setIsSubmittingDesign(false);
    }
  }

  async function submitRepair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = projectDir.trim();
    if (!trimmed) {
      return;
    }

    setIsSubmittingRepair(true);
    try {
      const response = await createRepairRun(trimmed);
      setSelectedRunId(response.runId);
      setProjectDir("");
      await refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Repair run failed");
    } finally {
      setIsSubmittingRepair(false);
    }
  }

  return (
    <section
      id="console"
      className="relative overflow-hidden bg-black px-4 py-20 sm:px-6 md:py-28"
    >
      <div className="bg-noise absolute inset-0 opacity-[0.12]" />
      <div className="relative mx-auto max-w-7xl">
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <p className="text-[10px] uppercase tracking-[0.35em] text-primary/65 sm:text-xs">
              Live console
            </p>
            <h2 className="mt-3 max-w-3xl text-4xl leading-[0.95] text-[#E1E0CC] sm:text-5xl md:text-6xl">
              Run the loop from the same surface that explains it.
            </h2>
          </div>
          <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-[#101010] px-4 py-2 text-sm text-gray-400">
            <Activity size={16} className="text-primary" />
            {runs.length} runs tracked
          </div>
        </div>

        {error ? (
          <div className="mt-6 flex items-start gap-3 rounded-lg border border-red-300/20 bg-red-300/10 p-4 text-sm text-red-100">
            <AlertTriangle className="mt-0.5 shrink-0" size={17} />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="mt-8 grid gap-4 lg:grid-cols-[420px_minmax(0,1fr)]">
          <div className="space-y-4">
            <form
              className="rounded-lg border border-white/10 bg-[#101010] p-5"
              onSubmit={submitDesign}
            >
              <div className="flex items-center gap-2 text-primary">
                <Sparkles size={17} />
                <h3 className="text-sm uppercase tracking-[0.25em]">
                  New design
                </h3>
              </div>
              <textarea
                className="mt-4 min-h-28 w-full resize-y rounded-md border border-white/10 bg-black/60 px-3 py-3 text-sm text-primary outline-none transition focus:border-primary/45"
                onChange={(event) => setRequirement(event.target.value)}
                placeholder="a 12V to 3.3V power board with a green LED"
                value={requirement}
              />
              <button
                className="group mt-3 inline-flex w-full items-center justify-between rounded-full bg-primary px-4 py-2 text-sm font-bold text-black disabled:cursor-wait disabled:opacity-60"
                disabled={isSubmittingDesign}
                type="submit"
              >
                {isSubmittingDesign ? "Dispatching" : "Generate and verify"}
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-black text-primary transition-transform group-hover:scale-110">
                  <ArrowRight size={16} />
                </span>
              </button>
            </form>

            <form
              className="rounded-lg border border-white/10 bg-[#101010] p-5"
              onSubmit={submitRepair}
            >
              <div className="flex items-center gap-2 text-primary">
                <CircuitBoard size={17} />
                <h3 className="text-sm uppercase tracking-[0.25em]">
                  Repair project
                </h3>
              </div>
              <input
                className="mt-4 w-full rounded-md border border-white/10 bg-black/60 px-3 py-3 text-sm text-primary outline-none transition focus:border-primary/45"
                onChange={(event) => setProjectDir(event.target.value)}
                placeholder="absolute path to a KiCad project directory"
                value={projectDir}
              />
              <button
                className="group mt-3 inline-flex w-full items-center justify-between rounded-full border border-primary/20 bg-transparent px-4 py-2 text-sm font-bold text-primary disabled:cursor-wait disabled:opacity-60"
                disabled={isSubmittingRepair}
                type="submit"
              >
                {isSubmittingRepair ? "Starting loop" : "Run auto-fix loop"}
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-black transition-transform group-hover:scale-110">
                  <ArrowRight size={16} />
                </span>
              </button>
            </form>

            <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
              <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
                Runs
              </h3>
              <div className="mt-4 space-y-2">
                {sortedRuns.length === 0 ? (
                  <p className="rounded-md border border-white/10 bg-black/35 p-4 text-sm text-gray-500">
                    No runs yet. Dispatch a design or repair loop to populate
                    this list.
                  </p>
                ) : (
                  sortedRuns.map((run) => (
                    <button
                      className={`w-full rounded-md border p-3 text-left transition ${
                        run.id === selectedRunId
                          ? "border-primary/45 bg-primary/10"
                          : "border-white/10 bg-black/35 hover:border-white/25"
                      }`}
                      key={run.id}
                      onClick={() => setSelectedRunId(run.id)}
                      type="button"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-sm text-[#E1E0CC]">
                          {run.kind ?? "fix"} / {shortId(run.id)}
                        </span>
                        <StatusBadge status={run.status} />
                      </div>
                      <div className="mt-2 flex items-center justify-between text-xs text-gray-500">
                        <span>{formatDate(run.createdAt)}</span>
                        <span>
                          score {run.finalScore ?? run.initialScore ?? "-"}
                        </span>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </div>
          </div>

          <RunDetail
            events={events}
            iterations={selectedIterations}
            recordEscalation={selectedRecord?.escalation}
            run={selectedRun}
          />
        </div>
      </div>
    </section>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  return (
    <span
      className={`rounded-full border px-2.5 py-1 text-[11px] font-bold ${statusClassName(status)}`}
    >
      {status ?? "unknown"}
    </span>
  );
}

function RunDetail({
  events,
  iterations,
  recordEscalation,
  run
}: {
  events: AtdpEvent[];
  iterations: NonNullable<ReturnType<typeof parseRunRecord>>["iterations"];
  recordEscalation?: unknown;
  run: DesignRun | null;
}) {
  if (!run) {
    return (
      <div className="rounded-lg border border-white/10 bg-[#101010] p-8 text-center text-gray-500">
        Select a run to inspect scorecards, repair rationale, and trajectory
        events.
      </div>
    );
  }

  const rationaleRows = (iterations ?? []).flatMap((iteration) =>
    Object.entries(iteration.patch_plan?.rationale ?? {}).map(([finding, why]) => ({
      finding,
      iteration: iteration.iteration,
      why
    }))
  );

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.25em] text-primary">
              Run {shortId(run.id)}
            </p>
            <h3 className="mt-3 text-3xl text-[#E1E0CC] sm:text-4xl">
              {run.kind ?? "fix"} control record
            </h3>
          </div>
          <StatusBadge status={run.status} />
        </div>
        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <Metric label="Final score" value={run.finalScore ?? "-"} />
          <Metric label="Initial score" value={run.initialScore ?? "-"} />
          <Metric label="Iterations" value={iterations?.length ?? 0} />
        </div>
        <div className="mt-5 space-y-2 text-xs text-gray-500">
          <p className="break-all">
            {run.requirement ? `requirement: ${run.requirement}` : `project: ${run.projectDir ?? "-"}`}
          </p>
          <p className="break-all">
            strategy: {run.strategyVersionId ?? "-"} / python run:{" "}
            {run.pythonRunId ?? "-"}
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
        <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
          Repair iterations
        </h3>
        {iterations && iterations.length > 0 ? (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[520px] text-left text-sm">
              <thead className="text-xs uppercase tracking-[0.18em] text-gray-500">
                <tr>
                  <th className="border-b border-white/10 py-3">#</th>
                  <th className="border-b border-white/10 py-3">Score</th>
                  <th className="border-b border-white/10 py-3">Delta</th>
                  <th className="border-b border-white/10 py-3">Ops</th>
                  <th className="border-b border-white/10 py-3">Resolved</th>
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {iterations.map((iteration) => (
                  <tr key={iteration.iteration}>
                    <td className="border-b border-white/5 py-3">
                      {iteration.iteration}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {iteration.scorecard.score ?? "-"}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {formatScoreDelta(iteration.score_delta)}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {iteration.patch_plan?.ops?.length ?? 0}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {iteration.resolved_findings?.length ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mt-4 rounded-md border border-white/10 bg-black/35 p-4 text-sm text-gray-500">
            No iteration record has been written for this run yet.
          </p>
        )}
      </div>

      {rationaleRows.length > 0 ? (
        <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
          <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
            Repair rationale
          </h3>
          <div className="mt-4 space-y-3">
            {rationaleRows.map((row) => (
              <div
                className="rounded-md border border-white/10 bg-black/35 p-3 text-sm"
                key={`${row.iteration}-${row.finding}`}
              >
                <p className="text-primary">
                  iter {row.iteration} / {row.finding}
                </p>
                <p className="mt-1 text-gray-400">{row.why}</p>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {recordEscalation ? (
        <div className="rounded-lg border border-amber-300/20 bg-amber-300/10 p-5">
          <h3 className="text-sm uppercase tracking-[0.25em] text-amber-100">
            Escalation
          </h3>
          <pre className="mt-4 max-h-56 overflow-auto rounded-md bg-black/45 p-4 text-xs text-amber-50">
            {JSON.stringify(recordEscalation, null, 2)}
          </pre>
        </div>
      ) : null}

      <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
        <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
          ATDP trajectory ({events.length})
        </h3>
        <div className="mt-4 max-h-[360px] space-y-2 overflow-y-auto pr-1">
          {events.length === 0 ? (
            <p className="rounded-md border border-white/10 bg-black/35 p-4 text-sm text-gray-500">
              No trajectory events are attached to this run yet.
            </p>
          ) : (
            events.map((event, index) => (
              <div
                className="grid gap-2 rounded-md border border-white/10 bg-black/35 p-3 text-sm md:grid-cols-[120px_70px_minmax(0,1fr)]"
                key={event.eventId ?? `${event.node}-${event.step}-${index}`}
              >
                <div className="text-primary">
                  {event.iteration}.{event.node ?? "node"}
                </div>
                <div
                  className={
                    event.reward === null || event.reward === undefined
                      ? "text-gray-600"
                      : event.reward >= 0
                        ? "text-emerald-200"
                        : "text-red-200"
                  }
                >
                  {event.reward === null || event.reward === undefined
                    ? "-"
                    : formatScoreDelta(event.reward)}
                </div>
                <div className="break-all text-gray-400">
                  {summarizeEvent(event)}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md border border-white/10 bg-black/35 p-4">
      <p className="text-xs uppercase tracking-[0.2em] text-gray-500">{label}</p>
      <p className="mt-2 text-3xl text-[#E1E0CC]">{value}</p>
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState("checking");
  const [healthError, setHealthError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getHealth()
      .then((response) => {
        if (active) {
          setHealth(response.status);
          setHealthError(null);
        }
      })
      .catch((err) => {
        if (active) {
          setHealth("offline");
          setHealthError(err instanceof Error ? err.message : "API offline");
        }
      });

    return () => {
      active = false;
    };
  }, []);

  return (
    <main className="min-h-screen bg-black text-[#E1E0CC]">
      <Hero health={health} healthError={healthError} />
      <SystemSection />
      <FeaturesSection />
      <section id="evolution" className="bg-black px-4 pb-4 sm:px-6">
        <div className="mx-auto max-w-7xl rounded-lg border border-white/10 bg-[#101010] p-5 text-sm text-gray-400 md:p-7">
          <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
            <div>
              <p className="text-[10px] uppercase tracking-[0.35em] text-primary/65">
                Evolution gate
              </p>
              <h2 className="mt-2 text-2xl text-[#E1E0CC] md:text-3xl">
                Candidate heuristics do not ship on vibes.
              </h2>
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              {["benchmarks", "promotion gates", "rollback"].map((item) => (
                <div
                  className="rounded-full border border-white/10 bg-black/35 px-4 py-2 text-center text-primary/80"
                  key={item}
                >
                  {item}
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>
      <ConsoleSection />
      <footer className="border-t border-white/10 bg-black px-4 py-8 text-center text-xs text-gray-600 sm:px-6">
        RatsNest control plane / KiCad design review, repair, and strategy evolution
      </footer>
    </main>
  );
}
