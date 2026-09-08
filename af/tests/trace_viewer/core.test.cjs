"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const Core = require(path.resolve(__dirname, "../../tools/trace-viewer/core.js"));
const fixtures = path.resolve(__dirname, "fixtures");

function read(name) {
  return fs.readFileSync(path.join(fixtures, name), "utf8");
}

test("v6 preserves structured lesson conditions and environment facts; mixed traces fail", () => {
  const events = read("valid-trace-v5.jsonl").trim().split("\n").map(JSON.parse);
  const lesson = {claim: "Wait safely.", applies_when: ["Work is pending."], exceptions: ["A deadline is near."]};
  for (const event of events) {
    event.schema_version = 6;
    if (event.kind === "decision_trace") {
      event.payload.context_projection.memory_brief.lessons = [lesson];
      event.payload.context_projection.environment_profile.facts = [{category: "time", statement: "Check deadlines."}];
    }
  }
  const encode = () => events.map(JSON.stringify).join("\n");
  const trace = Core.parseTraceJsonl(encode(), "v6.jsonl");
  assert.equal(trace.schemaVersion, 6);
  const row = Core.buildSession(trace, []).runs[0].rows[0];
  assert.deepEqual(row.memoryBrief.lessons, [lesson]);
  assert.equal(row.context.environment_profile.facts[0].statement, "Check deadlines.");
  events[0].schema_version = 5;
  assert.throws(() => Core.parseTraceJsonl(encode(), "mixed.jsonl"), /mixed trace schema/);
});

test("parses trace schema v5 and preserves action, memory, and observation linkage", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  assert.equal(trace.schemaVersion, 5);
  assert.equal(trace.runs.length, 1);
  assert.equal(trace.learningStreams.length, 1);
  assert.equal(trace.runs[0].decisionEvents.length, 2);

  const session = Core.buildSession(trace, []);
  const run = session.runs[0];
  const first = run.rows[0];
  assert.equal(first.iteration, 1);
  assert.equal(first.actionName, "advance_time");
  assert.equal(first.observation.action_kind, "advance_time");
  assert.equal(first.observation.summary, "Advanced <b>300s</b>");
  assert.equal(first.preObservation.action_kind, "start");
  assert.equal(first.preTime, "2030-03-01T00:00:00Z");
  assert.equal(first.postTime, "2030-03-01T00:05:00Z");
  assert.equal(first.simulationDeltaSeconds, 300);
  assert.equal(first.requestedAdvance, 300);
  assert.equal(first.appliedAdvance, 300);
  assert.equal(first.advanceMismatch, false);
  assert.equal(first.activeOperationDuringAdvance, true);
  assert.match(first.facts[0], /<img/);
  assert.deepEqual(first.memoryBrief.lessons, ["Wait for operation boundaries."]);
  assert.deepEqual(first.retrievalDiagnostics.selected_record_ids, ["lesson-1", "episode-1"]);
  assert.equal(first.closedEpisode.record_id, "episode-new");
  assert.equal(first.episodeCommit.new_revision, 2);
  assert.equal(first.recordedAt, "2026-08-29T00:00:01Z");
  assert.equal(trace.learningStreams.length, 1);
  assert.equal(trace.learningStreams[0].triggerRunId, "run-a");
  assert.deepEqual(trace.learningStreams[0].selectedRunIds, ["run-a"]);
  assert.deepEqual(trace.learningStreams[0].selectedEvidenceGroupIds, ["run-a"]);
});

test("accepts only SGR v2 decision envelopes", () => {
  const events = read("valid-trace-v5.jsonl").trim().split("\n").map(JSON.parse);
  const first = Core.buildSession(
    Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "sgr-v2.jsonl"),
    [],
  ).runs[0].rows[0];

  assert.equal(first.phase, "optimize");
  assert.equal(first.strategy, "observe one bounded interval");
  assert.deepEqual(first.competingHypotheses, ["advance to observe", "wait without advancing"]);

  events[1].payload.decision.envelope = {
    current_situation: "legacy",
    hypothesis: "legacy",
    remaining_steps: [],
    task_completed: false,
    action: { name: "advance_time", arguments: { duration_seconds: 300 } },
  };
  assert.throws(
    () => Core.parseTraceJsonl(events.map((event) => JSON.stringify(event)).join("\n") + "\n", "legacy.jsonl"),
    (error) =>
      error instanceof Core.ArtifactError &&
      error.details.code === "unsupported_sgr_envelope" &&
      /SGR v2/.test(error.message),
  );
});

test("links following verification, strategies and learning", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const run = Core.buildSession(trace, []).runs[0];

  assert.equal(run.rows[0].followingAssessment.status, "confirmed");
  assert.equal(run.rows[0].assessmentIteration, 2);
  assert.equal(run.rows[0].assessmentLinkStatus, "linked");
  assert.equal(run.rows[1].followingAssessment, null);
  assert.equal(run.rows[1].assessmentLinkStatus, "run_ended");
  assert.equal(run.strategyEpisodes.length, 2);
  assert.equal(run.strategyEpisodes[0].startedIteration, 1);
  assert.equal(run.learningOperations.length, 1);
  assert.equal(run.learningOperations[0].status, "finished");
  assert.equal(run.learningOperations[0].activations[0].lessonId, "lesson-new");
  assert.equal(run.timing.phases.find((phase) => phase.key === "memory_retrieval").durationSeconds, 0.03);
  assert.equal(run.timing.phases.find((phase) => phase.key === "learner_model").durationSeconds, 1.5);
});

test("folds only transparent alternating cadence runs", () => {
  function cadenceRow(iteration, actionName, net) {
    return {
      iteration,
      actionName,
      observation: {
        data: actionName === "advance_time"
          ? {
              interval_summary: {
                interval_kind: "observation",
                economic_inputs_complete: true,
                net_operating_delta_minor: net,
              },
            }
          : {},
      },
      failedObservation: false,
      policyRejected: false,
      providerOrSchemaFailure: false,
      incidentCodes: actionName === "get_logs" ? ["SERVER_CAPACITY_EXCEEDED"] : [],
      followingAssessment: { status: "confirmed" },
    };
  }
  const rows = [
    cadenceRow(1, "advance_time", 10),
    cadenceRow(2, "get_logs", null),
    cadenceRow(3, "advance_time", -5),
    cadenceRow(4, "get_logs", null),
  ];
  const folded = Core.foldCadenceRows(rows);

  assert.equal(folded.length, 1);
  assert.equal(folded[0].kind, "cadence");
  assert.equal(folded[0].cycles, 2);

  rows[2].followingAssessment = { status: "contradicted" };
  assert.equal(Core.foldCadenceRows(rows).every((item) => item.kind === "decision"), true);
});

test("keeps runner, simulator, budget, and terminal facts independent", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const run = Core.buildSession(trace, []).runs[0];
  assert.deepEqual(run.lifecycle, {
    runner: "finished",
    simulator: "running",
    simulatorCompleted: false,
    budgetReached: true,
    terminalDecision: true,
    terminalObservation: true,
  });
  assert.equal(run.stopReason, "iteration budget reached");
});

test("groups several stream ids without sharing sequences", () => {
  const source = read("valid-trace-v5.jsonl").trim().split("\n").map(JSON.parse);
  const runEvents = source.filter((event) => event.stream_kind === "run");
  const second = runEvents.map((event) => {
    const copy = structuredClone(event);
    copy.stream_id = "run:run-two";
    if (copy.payload.run_id) copy.payload.run_id = "run-two";
    if (copy.payload.result) copy.payload.result.run_id = "run-two";
    return copy;
  });
  const trace = Core.parseTraceJsonl(
    runEvents.concat(second).map((event) => JSON.stringify(event)).join("\n") + "\n",
    "multi-run-v5.jsonl",
  );
  assert.deepEqual(
    trace.runs.map((run) => run.runId),
    ["run-a", "run-two"],
  );
  assert.equal(trace.runs[0].events[0].event.sequence, 1);
  assert.equal(trace.runs[1].events[0].event.sequence, 1);
  assert.equal(trace.runs[1].finishedEvent.event.payload.result.status, "running");
});

test("reports invalid and mixed JSONL clearly", () => {
  assert.throws(
    () => Core.parseTraceJsonl(read("invalid.jsonl"), "invalid.jsonl"),
    (error) => error instanceof Core.ArtifactError && error.details.line === 1 && /invalid JSON/.test(error.message),
  );
  assert.throws(
    () => Core.parseTraceJsonl(read("mixed.jsonl"), "mixed.jsonl"),
    (error) => error instanceof Core.ArtifactError && error.details.code === "mixed_schema",
  );
  assert.throws(
    () => {
      const event = JSON.parse(read("valid-trace-v5.jsonl").split("\n")[0]);
      event.schema_version = 4;
      return Core.parseTraceJsonl(JSON.stringify(event), "unsupported.jsonl");
    },
    /unsupported trace schema version 4/,
  );
});

test("classifies and links optional artifacts while preserving missing fields", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const companions = [
    Core.parseCompanionJson(read("summary-v5.json"), "summary-v5.json"),
    Core.parseCompanionJson(read("manifest-minimal.json"), "manifest-minimal.json"),
    Core.parseCompanionJson(read("spec-v3.json"), "spec-v3.json"),
    Core.parseCompanionJson(read("decision-corpus-v3.json"), "decision-corpus-v3.json"),
  ];
  assert.deepEqual(
    companions.map((item) => item.kind),
    ["summary", "manifest", "spec", "corpus"],
  );
  const session = Core.buildSession(trace, companions);
  const run = session.runs[0];
  assert.equal(run.manifest.finished_at, null);
  assert.equal(run.summaryAttempt.seed, 7);
  assert.equal(run.corpusRun.decisions[0].decision_id, "d".repeat(64));
  assert.equal(session.spec.experiment_id, "fixture-experiment");
});

test("preserves unsafe integer money and formats exact raw value", () => {
  assert.equal(Core.supportsSourceContext(), true);
  const artifact = Core.parseCompanionJson(read("unsafe-money.json"), "unsafe-money.json");
  assert.equal(typeof artifact.value.total_cost_minor, "bigint");
  assert.equal(artifact.value.total_cost_minor, 9007199254740993n);
  const formatted = Core.formatMinor(artifact.value.total_cost_minor);
  assert.equal(formatted.raw, "9007199254740993");
  assert.equal(formatted.chartValue, null);
  assert.match(formatted.compact, /minor$/);

  const decimal = Core.formatMinor(1100.5);
  assert.equal(decimal.raw, "1100.5");
  assert.equal(decimal.chartValue, 1100.5);
});

test("filters literal text and diagnostic categories", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const rows = Core.buildSession(trace, []).runs[0].rows;
  assert.deepEqual(Core.filterDecisionRows(rows, { action: "finish" }).map((row) => row.iteration), [2]);
  assert.deepEqual(Core.filterDecisionRows(rows, { groups: ["advances"] }).map((row) => row.iteration), [1]);
  assert.deepEqual(Core.filterDecisionRows(rows, { terminal: true }).map((row) => row.iteration), [2]);
  assert.deepEqual(Core.filterDecisionRows(rows, { text: "<IMG SRC=X" }).map((row) => row.iteration), [1]);
  assert.deepEqual(Core.filterDecisionRows(rows, { minIteration: 2, maxIteration: 2 }).map((row) => row.iteration), [2]);
});

test("uses failure telemetry once when a decision is missing", () => {
  const item = {
    rawLine: "{}",
    lineNumber: 2,
    event: {
      kind: "decision_trace",
      payload: {
        iteration: 1,
        context_projection: {
          environment_state: { decision_view: {}, latest_observation: null },
          agent_working_state: {},
          memory_brief: {},
        },
        capabilities: { items: [] },
        decision: null,
        policy_result: null,
        observation: null,
        failure: {
          stage: "reasoner",
          category: "invalid_output",
          message: "bad output",
          telemetry: {
            duration_seconds: 2,
            token_usage: { input_tokens: 9, output_tokens: null, total_tokens: 9 },
          },
        },
        model_duration_seconds: 2,
        environment_duration_seconds: 0,
      },
    },
  };
  const row = Core.deriveDecisionRow(item);
  assert.equal(row.decision, null);
  assert.equal(row.telemetry.duration_seconds, 2);
  assert.equal(row.tokenUsage.input_tokens, 9);
  assert.equal(row.providerOrSchemaFailure, true);
});

test("does not carry missing cumulative telemetry through a series", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const run = Core.buildSession(trace, []).runs[0];
  const input = run.charts.tokens.find((series) => series.name === "input");
  assert.equal(input.incomplete, false);
  assert.deepEqual(input.points.map((point) => point.value), [10, 22]);
  const cached = run.charts.tokens.find((series) => series.name === "reasoning");
  assert.equal(cached.incomplete, false);
  assert.deepEqual(cached.points.map((point) => point.value), [2, 3]);
});

test("compares iteration budget with the saved simulator horizon", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const run = Core.buildSession(trace, []).runs[0];
  const budget = run.charts.budgetHorizon.find((series) => series.name === "iteration budget used %");
  const horizon = run.charts.budgetHorizon.find((series) => series.name === "simulation horizon elapsed %");

  assert.deepEqual(budget.points.map((point) => point.value), [50, 100]);
  assert.equal(horizon.points[0].value, (300 / 2678400) * 100);
  assert.equal(horizon.points[1].value, null);
  assert.equal(horizon.incomplete, true);
});

test("distinguishes structured incident evidence from free-text heuristics", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const item = structuredClone(trace.runs[0].decisionEvents[0]);
  item.event.payload.observation.summary = "Saw DDOS_MITIGATION_REQUIRED in a note";
  item.event.payload.observation.data = {};

  const heuristic = Core.deriveDecisionRow(item);
  assert.deepEqual(heuristic.incidentCodes, []);
  assert.deepEqual(heuristic.freeTextIncidentCodes, ["DDOS_MITIGATION_REQUIRED"]);

  item.event.payload.observation.data.error = "DDOS_MITIGATION_REQUIRED";
  const structured = Core.deriveDecisionRow(item);
  assert.deepEqual(structured.incidentCodes, ["DDOS_MITIGATION_REQUIRED"]);
  assert.deepEqual(structured.freeTextIncidentCodes, []);
});

test("projects a decision map without inventing alternative or causal edges", () => {
  const trace = Core.parseTraceJsonl(read("valid-trace-v5.jsonl"), "valid-trace-v5.jsonl");
  const run = Core.buildSession(trace, []).runs[0];
  const map = run.decisionMap;

  assert.equal(map.nodes.length, 2);
  assert.equal(map.nodes[0].evidenceSummary, "started");
  assert.equal(map.nodes[0].hypothesis, "observe one bounded interval");
  assert.equal(map.nodes[0].actionName, "advance_time");
  assert.equal(map.nodes[0].observationSummary, "Advanced <b>300s</b>");
  assert.deepEqual(map.chronology, [
    { fromIteration: 1, toIteration: 2, kind: "recorded_sequence" },
  ]);
});

test("v6 uses published final_state outcomes and keeps terminal site status separate", () => {
  const run = Core.buildSession(Core.parseTraceJsonl(read("valid-trace-v6.jsonl"), "v6.jsonl"), []).runs[0];
  assert.equal(run.rows[0].groups.has("advances"), true);
  assert.equal(run.rows[0].requestedAdvance, 300);
  assert.equal(run.outcomeMetrics.score, 100);
  assert.equal(run.outcomeMetrics.total_cost_minor, 2081738912);
  assert.equal(run.outcomeMetrics.uptime_ratio, 0.993889);
  assert.equal(run.outcomeMetrics.slo_passed, true);
  assert.equal(run.outcomeMetrics.site_status, "unavailable");
  assert.equal(run.lifecycle.simulatorCompleted, true);
  assert.equal(run.profitStory, undefined);
  assert.equal(run.charts.budgetHorizon.some(series => series.name.includes("budget")), false);
  const missing = Core.collectOutcomeMetrics({final_state: {costs: {total_cost_minor: 0}}});
  assert.equal(missing.total_cost_minor, 0);
  assert.equal(missing.score, null);
  assert.equal(missing.uptime_ratio, null);
});

test("nested batch and program clocks retain exact timestamps without inventing missing data", () => {
  const source = JSON.parse(read("valid-trace-v6.jsonl").split("\n").find(line => JSON.parse(line).kind === "decision_trace"));
  const earlier = {simulation_time: "2030-03-01T00:03:00Z"};
  const later = {simulation_time: "2030-03-01T00:05:00Z"};
  const row = data => {
    source.payload.observation.data = data;
    return Core.deriveDecisionRow({event: source});
  };
  const batch = row({items: [
    {observation: {data: {clock: later}}},
    {observation: {data: {clock: earlier}}},
  ]});
  assert.equal(batch.postTime, later.simulation_time);
  assert.equal(batch.simulationDeltaSeconds, 300);
  assert.equal(row({results: [{selected: {"data.clock": later}}]}).postTime, later.simulation_time);
  assert.equal(row({results: [{selected: {data: {clock: earlier}}}]}).postTime, earlier.simulation_time);
  assert.equal(row({results: [{selected: {"data.costs": {total_cost_minor: 123}}}]}).postTime, null);
});


test("ad-hoc corpus v3 leaves experiment, seed and repeat provenance absent", () => {
  const corpus = Core.parseCompanionJson(read("ad-hoc-corpus-v3.json"), "ad-hoc.json");
  assert.equal(corpus.kind, "corpus");
  const session = Core.buildSession(Core.parseTraceJsonl(read("valid-trace-v6.jsonl"), "trace.jsonl"), [corpus]);
  assert.equal(session.spec, null);
  assert.equal(session.runs[0].spec, null);
  assert.equal(session.runs[0].manifest.seed, null);
  assert.equal(session.runs[0].manifest.repeat_index, null);
  assert.equal(session.runs[0].corpusRun.decisions[0].seed, null);
  assert.equal(session.runs[0].corpusRun.decisions[0].repeat_index, null);
  assert.equal(session.runs[0].corpusRun.manifest.experiment_id, undefined);
  const invalid = JSON.parse(read("ad-hoc-corpus-v3.json"));
  invalid.resolved_spec_sha256 = "a".repeat(64);
  assert.throws(() => Core.parseCompanionJson(JSON.stringify(invalid), "invalid.json"), /must be present together/);
});
