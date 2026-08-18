export interface RatsNestDecisionOption {
  key: string;
  label: string;
  value: string;
  basis: string;
  ackToken: string;
  freeText: boolean;
}

export interface RatsNestDecision {
  slot: string;
  question: string;
  kind: string;
  options: RatsNestDecisionOption[];
  recommendedKey: string;
  citation: string;
}

export interface RatsNestDecisionBlock {
  prose: string;
  decisions: RatsNestDecision[];
}

const FENCE = /(?:^|\n)```ratsnest-decisions[ \t]*\r?\n([\s\S]*?)\r?\n```(?=$|\r?\n)/g;
const SLOT = /^[A-Za-z_][A-Za-z0-9_]*$/;
const KEY = /^[A-Za-z0-9_.+-]{1,32}$/;

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value).sort();
  return actual.length === keys.length && keys.slice().sort().every((key, index) => key === actual[index]);
}

function boundedString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length <= maximum;
}

/** Parse only the exact fenced contract emitted by the legacy Decisions kernel. */
export function parseRatsNestDecisionBlock(content: string): RatsNestDecisionBlock | null {
  if (!content || content.length > 200_000) return null;
  const matches = [...content.matchAll(FENCE)];
  if (matches.length !== 1 || matches[0][1].length > 100_000) return null;
  let decoded: unknown;
  try { decoded = JSON.parse(matches[0][1]); } catch { return null; }
  const envelope = object(decoded);
  if (!envelope || !exactKeys(envelope, ["decisions"]) || !Array.isArray(envelope.decisions) || envelope.decisions.length < 1 || envelope.decisions.length > 20) return null;

  const slots = new Set<string>();
  const decisions: RatsNestDecision[] = [];
  for (const rawDecision of envelope.decisions) {
    const decision = object(rawDecision);
    if (!decision || !exactKeys(decision, ["slot", "question", "kind", "options", "recommended_key", "citation"])) return null;
    if (!boundedString(decision.slot, 100) || !SLOT.test(decision.slot) || slots.has(decision.slot) ||
        !boundedString(decision.question, 10_000) || !decision.question.trim() ||
        !boundedString(decision.kind, 100) || !boundedString(decision.recommended_key, 32) ||
        !boundedString(decision.citation, 10_000) || !Array.isArray(decision.options) ||
        decision.options.length < 1 || decision.options.length > 6) return null;
    slots.add(decision.slot);
    const optionKeys = new Set<string>();
    const options: RatsNestDecisionOption[] = [];
    for (const rawOption of decision.options) {
      const option = object(rawOption);
      if (!option || !exactKeys(option, ["key", "label", "value", "basis", "ack_token", "free_text"]) ||
          !boundedString(option.key, 32) || !KEY.test(option.key) || optionKeys.has(option.key.toUpperCase()) ||
          !boundedString(option.label, 2_000) || !option.label.trim() || !boundedString(option.value, 10_000) ||
          !boundedString(option.basis, 10_000) || !boundedString(option.ack_token, 2_000) || typeof option.free_text !== "boolean") return null;
      optionKeys.add(option.key.toUpperCase());
      options.push({ key: option.key, label: option.label, value: option.value, basis: option.basis, ackToken: option.ack_token, freeText: option.free_text });
    }
    if (decision.recommended_key && !optionKeys.has(decision.recommended_key.toUpperCase())) return null;
    decisions.push({ slot: decision.slot, question: decision.question, kind: decision.kind, options, recommendedKey: decision.recommended_key, citation: decision.citation });
  }
  const match = matches[0];
  const prose = `${content.slice(0, match.index)}${content.slice((match.index ?? 0) + match[0].length)}`.trim();
  return { prose, decisions };
}

export function canonicalDecisionReply(
  decisions: RatsNestDecision[],
  selections: Record<string, string>,
  freeText: Record<string, string>,
): string | null {
  const lines: string[] = [];
  const supplied: string[] = [];
  for (const decision of decisions) {
    const key = selections[decision.slot];
    if (!key) continue;
    const option = decision.options.find((candidate) => candidate.key.toUpperCase() === key.toUpperCase());
    if (!option) return null;
    lines.push(`PICK: ${decision.slot}=${option.key.toUpperCase()}`);
    if (option.freeText) {
      const text = (freeText[decision.slot] ?? "").trim();
      if (!text || text.length > 10_000) return null;
      supplied.push(text);
    }
  }
  // The legacy parser has one free-text remainder per turn; leave further
  // custom decisions open so it can ask for them on subsequent turns.
  if (lines.length === 0 || supplied.length > 1) return null;
  return supplied.length ? `${lines.join("\n")}\n\n${supplied[0]}` : lines.join("\n");
}
