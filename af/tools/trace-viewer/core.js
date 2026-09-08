(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.UptickTraceViewerCore = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  const TRACE_SCHEMA_VERSION = 6;
  const RUN_KINDS = new Set([
    "run_started",
    "decision_trace",
    "episode_closed",
    "episode_committed",
    "run_failed",
    "run_finished",
  ]);
  const LEARNING_KINDS = new Set([
    "learning_started",
    "lesson_evaluated",
    "lesson_activated",
    "learning_failed",
    "learning_finished",
  ]);
  const ACTIVE_OPERATION_STATUSES = new Set(["accepted", "pending", "running", "in_progress"]);
  const STRUCTURED_INCIDENT_CODES = new Set([
    "DDOS_MITIGATION_REQUIRED",
    "SERVER_CAPACITY_EXCEEDED",
  ]);
  const SAFE_INTEGER_LIMIT = BigInt(Number.MAX_SAFE_INTEGER);
  const COMPANION_ROLES = new Set(["summary", "spec", "corpus", "comparison", "bootstrap"]);
  const DECISION_PHASES = new Set([
    "observe",
    "diagnose",
    "mitigate",
    "verify",
    "optimize",
    "finish",
  ]);

  class ArtifactError extends Error {
    constructor(message, details) {
      super(message);
      this.name = "ArtifactError";
      this.details = details || null;
    }
  }

  let sourceContextSupport;

  function supportsSourceContext() {
    if (sourceContextSupport !== undefined) {
      return sourceContextSupport;
    }
    let observed = null;
    JSON.parse("9007199254740993", function (_key, value, context) {
      if (context && typeof context.source === "string") {
        observed = context.source;
      }
      return value;
    });
    sourceContextSupport = observed === "9007199254740993";
    return sourceContextSupport;
  }

  function scanNumberTokens(text) {
    const values = [];
    let index = 0;
    let inString = false;
    let escaped = false;
    while (index < text.length) {
      const character = text[index];
      if (inString) {
        if (escaped) {
          escaped = false;
        } else if (character === "\\") {
          escaped = true;
        } else if (character === '"') {
          inString = false;
        }
        index += 1;
        continue;
      }
      if (character === '"') {
        inString = true;
        index += 1;
        continue;
      }
      if (character === "-" || (character >= "0" && character <= "9")) {
        const match = text.slice(index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
        if (match) {
          values.push({ raw: match[0], offset: index });
          index += match[0].length;
          continue;
        }
      }
      index += 1;
    }
    return values;
  }

  function isUnsafeIntegerToken(raw) {
    if (/^-?\d+$/.test(raw)) {
      const value = BigInt(raw);
      return value > SAFE_INTEGER_LIMIT || value < -SAFE_INTEGER_LIMIT;
    }
    const approximate = Number(raw);
    return Number.isFinite(approximate) && Number.isInteger(approximate) && !Number.isSafeInteger(approximate);
  }

  function parseJsonLossless(text, sourceName) {
    const source = sourceName || "artifact";
    const canRecoverSource = supportsSourceContext();
    if (!canRecoverSource) {
      const unsafe = scanNumberTokens(text).find(function (token) {
        return isUnsafeIntegerToken(token.raw);
      });
      if (unsafe) {
        throw new ArtifactError(
          source +
            ": this browser cannot preserve unsafe integer " +
            unsafe.raw +
            " exactly; use a browser with JSON source-context support",
          { source: source, offset: unsafe.offset, code: "unsafe_integer_unsupported" },
        );
      }
    }
    try {
      return JSON.parse(text, function (_key, value, context) {
        if (
          typeof value === "number" &&
          context &&
          typeof context.source === "string" &&
          isUnsafeIntegerToken(context.source)
        ) {
          if (/^-?\d+$/.test(context.source)) {
            return BigInt(context.source);
          }
          throw new ArtifactError(
            source + ": unsafe non-integer numeric token cannot be represented exactly: " + context.source,
            { source: source, code: "unsafe_decimal" },
          );
        }
        return value;
      });
    } catch (error) {
      if (error instanceof ArtifactError) {
        throw error;
      }
      throw new ArtifactError(source + ": invalid JSON: " + error.message, {
        source: source,
        code: "invalid_json",
      });
    }
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isSafeInteger(value) {
    return typeof value === "number" && Number.isSafeInteger(value);
  }

  function requireObject(value, message, details) {
    if (!isObject(value)) {
      throw new ArtifactError(message, details);
    }
  }

  function parseTraceJsonl(text, sourceName) {
    const source = sourceName || "trace.jsonl";
    const parsedLines = [];
    const versions = new Map();
    const lines = text.split(/\r?\n/);
    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const rawLine = lines[lineIndex];
      if (!rawLine.trim()) {
        continue;
      }
      let event;
      try {
        event = parseJsonLossless(rawLine, source + ":" + (lineIndex + 1));
      } catch (error) {
        if (error instanceof ArtifactError) {
          error.details = Object.assign({}, error.details, { line: lineIndex + 1 });
        }
        throw error;
      }
      if (!isObject(event)) {
        throw new ArtifactError(source + ":" + (lineIndex + 1) + ": event must be an object", {
          source: source,
          line: lineIndex + 1,
          code: "invalid_envelope",
        });
      }
      if (!Object.prototype.hasOwnProperty.call(event, "schema_version")) {
        throw new ArtifactError(source + ":" + (lineIndex + 1) + ": schema_version is required", {
          source: source,
          line: lineIndex + 1,
          code: "invalid_envelope",
        });
      }
      const versionKey = String(event.schema_version);
      if (!versions.has(versionKey)) {
        versions.set(versionKey, []);
      }
      versions.get(versionKey).push(lineIndex + 1);
      parsedLines.push({ event: event, rawLine: rawLine, lineNumber: lineIndex + 1 });
    }

    if (parsedLines.length === 0) {
      throw new ArtifactError(source + ": trace is empty", {
        source: source,
        code: "empty_trace",
      });
    }
    if (versions.size > 1) {
      const description = Array.from(versions.entries())
        .map(function (entry) {
          return entry[0] + " at line" + (entry[1].length === 1 ? " " : "s ") + entry[1].join(", ");
        })
        .join("; ");
      throw new ArtifactError(source + ": mixed trace schema versions: " + description, {
        source: source,
        code: "mixed_schema",
      });
    }
    const onlyVersion = parsedLines[0].event.schema_version;
    if (onlyVersion !== 5 && onlyVersion !== TRACE_SCHEMA_VERSION) {
      throw new ArtifactError(source + ": unsupported trace schema version " + String(onlyVersion), {
        source: source,
        code: "unsupported_schema",
      });
    }

    const streamOrder = [];
    const streamsById = new Map();
    for (const parsed of parsedLines) {
      const event = parsed.event;
      const envelopeDetails = { source: source, line: parsed.lineNumber, code: "invalid_envelope" };
      if (typeof event.stream_id !== "string" || event.stream_id.length === 0) {
        throw new ArtifactError(source + ":" + parsed.lineNumber + ": stream_id must be a non-empty string", envelopeDetails);
      }
      if (event.stream_kind !== "run" && event.stream_kind !== "learning") {
        throw new ArtifactError(source + ":" + parsed.lineNumber + ": stream_kind must be run or learning", envelopeDetails);
      }
      if (!isSafeInteger(event.sequence) || event.sequence < 1) {
        throw new ArtifactError(source + ":" + parsed.lineNumber + ": sequence must be a positive safe integer", envelopeDetails);
      }
      if (typeof event.recorded_at !== "string" || Number.isNaN(Date.parse(event.recorded_at))) {
        throw new ArtifactError(source + ":" + parsed.lineNumber + ": recorded_at must be an ISO timestamp", envelopeDetails);
      }
      const supportedKinds = event.stream_kind === "run" ? RUN_KINDS : LEARNING_KINDS;
      if (!supportedKinds.has(event.kind)) {
        throw new ArtifactError(source + ":" + parsed.lineNumber + ": unsupported event kind " + String(event.kind), envelopeDetails);
      }
      requireObject(event.payload, source + ":" + parsed.lineNumber + ": payload must be an object", envelopeDetails);
      validateUsedPayload(event, source, parsed.lineNumber);

      if (!streamsById.has(event.stream_id)) {
        streamsById.set(event.stream_id, {
          streamId: event.stream_id,
          streamKind: event.stream_kind,
          events: [],
          warnings: [],
        });
        streamOrder.push(event.stream_id);
      } else if (streamsById.get(event.stream_id).streamKind !== event.stream_kind) {
        throw new ArtifactError(
          source + ":" + parsed.lineNumber + ": stream_kind changed inside " + event.stream_id,
          envelopeDetails,
        );
      }
      streamsById.get(event.stream_id).events.push({
        event: event,
        rawLine: parsed.rawLine,
        lineNumber: parsed.lineNumber,
      });
    }

    const streams = streamOrder.map(function (streamId) {
      const stream = streamsById.get(streamId);
      return stream.streamKind === "run" ? finalizeRun(stream) : finalizeLearningStream(stream);
    });
    const runs = streams.filter(function (stream) { return stream.streamKind === "run"; });
    const learningStreams = streams.filter(function (stream) { return stream.streamKind === "learning"; });
    return {
      kind: "trace",
      schemaVersion: onlyVersion,
      sourceName: source,
      runs: runs,
      learningStreams: learningStreams,
      streams: streams,
      warnings: streams.flatMap(function (stream) {
        return stream.warnings;
      }),
      rawSize: text.length,
    };
  }

  function validateUsedPayload(event, source, lineNumber) {
    const prefix = source + ":" + lineNumber + ": ";
    const details = { source: source, line: lineNumber, code: "invalid_payload" };
    if (event.kind === "run_started") {
      if (typeof event.payload.run_id !== "string" || event.payload.run_id.length === 0) {
        throw new ArtifactError(prefix + "run_started.payload.run_id is required", details);
      }
      requireObject(event.payload.initial_state, prefix + "run_started.payload.initial_state must be an object", details);
      if (event.payload.memory_view !== null && event.payload.memory_view !== undefined) {
        requireObject(event.payload.memory_view, prefix + "run_started.payload.memory_view must be an object or null", details);
      }
      return;
    }
    if (event.kind === "decision_trace") {
      if (!isSafeInteger(event.payload.iteration) || event.payload.iteration < 1) {
        throw new ArtifactError(prefix + "decision_trace.payload.iteration must be a positive safe integer", details);
      }
      requireObject(event.payload.context_projection, prefix + "decision_trace.payload.context_projection must be an object", details);
      requireObject(
        event.payload.context_projection.environment_state,
        prefix + "decision_trace.payload.context_projection.environment_state must be an object",
        details,
      );
      requireObject(
        event.payload.context_projection.agent_working_state,
        prefix + "SGR v2 requires decision_trace.payload.context_projection.agent_working_state",
        Object.assign({}, details, { code: "unsupported_sgr_envelope" }),
      );
      requireObject(
        event.payload.context_projection.memory_brief,
        prefix + "decision_trace.payload.context_projection.memory_brief must be an object",
        details,
      );
      requireObject(event.payload.capabilities, prefix + "decision_trace.payload.capabilities must be an object", details);
      if (event.payload.decision !== null && event.payload.decision !== undefined) {
        requireObject(event.payload.decision, prefix + "decision must be an object or null", details);
        requireObject(event.payload.decision.envelope, prefix + "decision.envelope must be an object", details);
        validateSgrV2Envelope(event.payload.decision.envelope, prefix, details);
      }
      return;
    }
    if (event.kind === "episode_closed") {
      requireObject(event.payload.episode, prefix + "episode_closed.payload.episode must be an object", details);
      return;
    }
    if (event.kind === "episode_committed") {
      requireObject(event.payload.commit, prefix + "episode_committed.payload.commit must be an object", details);
      return;
    }
    if (event.kind === "run_failed") {
      if (typeof event.payload.failure_stage !== "string") {
        throw new ArtifactError(prefix + "run_failed payload requires failure_stage", details);
      }
      requireObject(event.payload.failure, prefix + "run_failed.payload.failure must be an object", details);
      return;
    }
    if (event.kind === "run_finished") {
      requireObject(event.payload.result, prefix + "run_finished.payload.result must be an object", details);
      requireObject(event.payload.metrics, prefix + "run_finished.payload.metrics must be an object", details);
      return;
    }
    if (event.kind === "learning_started") {
      if (typeof event.payload.trigger_run_id !== "string" || !event.payload.trigger_run_id.length) {
        throw new ArtifactError(prefix + "learning_started.payload.trigger_run_id is required", details);
      }
      if (
        event.payload.trigger === "after_closed_episode" &&
        (typeof event.payload.trigger_episode_id !== "string" || !event.payload.trigger_episode_id.length)
      ) {
        throw new ArtifactError(
          prefix + "after_closed_episode learning_started requires trigger_episode_id",
          details,
        );
      }
      requireObject(event.payload.base_view, prefix + "learning_started.payload.base_view must be an object", details);
      return;
    }
    if (event.kind === "lesson_evaluated") {
      requireObject(event.payload.batch, prefix + "lesson_evaluated.payload.batch must be an object", details);
      return;
    }
    if (event.kind === "lesson_activated") {
      requireObject(event.payload.commit, prefix + "lesson_activated.payload.commit must be an object", details);
      return;
    }
    if (event.kind === "learning_failed") {
      requireObject(event.payload.failure, prefix + "learning_failed.payload.failure must be an object", details);
      return;
    }
    if (event.kind === "learning_finished") {
      requireObject(event.payload.result, prefix + "learning_finished.payload.result must be an object", details);
    }
  }

  function validateSgrV2Envelope(envelope, prefix, details) {
    const sgrDetails = Object.assign({}, details, { code: "unsupported_sgr_envelope" });
    const requiredArrays = [
      ["facts", true],
      ["competing_hypotheses", false],
      ["contradicting_evidence", false],
      ["expected_result", true],
      ["verification", true],
    ];
    if (!DECISION_PHASES.has(envelope.phase)) {
      throw new ArtifactError(
        prefix + "SGR v2 decision.envelope.phase is missing or unsupported",
        sgrDetails,
      );
    }
    for (const definition of requiredArrays) {
      const value = envelope[definition[0]];
      if (!Array.isArray(value) || (definition[1] && value.length === 0)) {
        throw new ArtifactError(
          prefix + "SGR v2 decision.envelope." + definition[0] + " must be " +
            (definition[1] ? "a non-empty array" : "an array"),
          sgrDetails,
        );
      }
    }
    requireObject(
      envelope.previous_verification,
      prefix + "SGR v2 decision.envelope.previous_verification must be an object",
      sgrDetails,
    );
    if (typeof envelope.strategy !== "string" || envelope.strategy.length === 0) {
      throw new ArtifactError(prefix + "SGR v2 decision.envelope.strategy is required", sgrDetails);
    }
    requireObject(
      envelope.selected_action,
      prefix + "SGR v2 decision.envelope.selected_action must be an object",
      sgrDetails,
    );
    if (typeof envelope.selected_action.name !== "string" || envelope.selected_action.name.length === 0) {
      throw new ArtifactError(
        prefix + "SGR v2 decision.envelope.selected_action.name is required",
        sgrDetails,
      );
    }
    requireObject(
      envelope.selected_action.arguments,
      prefix + "SGR v2 decision.envelope.selected_action.arguments must be an object",
      sgrDetails,
    );
    if (typeof envelope.task_completed !== "boolean") {
      throw new ArtifactError(
        prefix + "SGR v2 decision.envelope.task_completed must be boolean",
        sgrDetails,
      );
    }
  }

  function finalizeRun(run) {
    const inputEvents = run.events.slice();
    const sequenceSeen = new Set();
    const iterationSeen = new Set();
    let previousSequence = null;
    for (const item of inputEvents) {
      const sequence = item.event.sequence;
      if (sequenceSeen.has(sequence)) {
        run.warnings.push(run.streamId + ": duplicate sequence " + sequence);
      }
      if (previousSequence !== null && sequence <= previousSequence) {
        run.warnings.push(
          run.streamId + ": non-monotonic sequence " + sequence + " after " + previousSequence,
        );
      }
      sequenceSeen.add(sequence);
      previousSequence = sequence;
      if (item.event.kind === "decision_trace") {
        const iteration = item.event.payload.iteration;
        if (iterationSeen.has(iteration)) {
          run.warnings.push(run.streamId + ": duplicate iteration " + iteration);
        }
        iterationSeen.add(iteration);
      }
    }
    const orderedSequences = Array.from(sequenceSeen).sort(function (left, right) {
      return left - right;
    });
    for (let index = 1; index < orderedSequences.length; index += 1) {
      if (orderedSequences[index] !== orderedSequences[index - 1] + 1) {
        run.warnings.push(
          run.streamId + ": sequence gap after " + orderedSequences[index - 1],
        );
      }
    }

    const events = inputEvents.slice().sort(function (left, right) {
      if (left.event.sequence === right.event.sequence) {
        return left.lineNumber - right.lineNumber;
      }
      return left.event.sequence - right.event.sequence;
    });
    const starts = events.filter(function (item) {
      return item.event.kind === "run_started";
    });
    const finishes = events.filter(function (item) {
      return item.event.kind === "run_finished";
    });
    const failures = events.filter(function (item) {
      return item.event.kind === "run_failed";
    });
    if (starts.length !== 1) {
      run.warnings.push(run.streamId + ": expected one run_started event, found " + starts.length);
    }
    if (finishes.length > 1) {
      run.warnings.push(run.streamId + ": duplicate run_finished events");
    }
    if (failures.length > 1) {
      run.warnings.push(run.streamId + ": duplicate run_failed events");
    }
    if (finishes.length && failures.length) {
      run.warnings.push(run.streamId + ": both run_finished and run_failed are present");
    }
    const payloadRunIds = uniqueStrings(events.map(function (item) { return item.event.payload.run_id; }));
    const runId = payloadRunIds[0] || run.streamId.replace(/^run:/, "");
    if (payloadRunIds.length > 1) {
      run.warnings.push(run.streamId + ": conflicting payload run IDs: " + payloadRunIds.join(", "));
    }
    if (run.streamId !== "run:" + runId) {
      run.warnings.push(run.streamId + ": stream ID does not match payload run ID " + runId);
    }
    run.runId = runId;
    run.events = events;
    run.startedEvent = starts[0] || null;
    run.finishedEvent = finishes[finishes.length - 1] || null;
    run.failedEvent = failures[failures.length - 1] || null;
    run.decisionEvents = events.filter(function (item) {
      return item.event.kind === "decision_trace";
    });
    run.episodeEvents = events.filter(function (item) {
      return item.event.kind === "episode_closed" || item.event.kind === "episode_committed";
    });
    return run;
  }

  function finalizeLearningStream(stream) {
    const events = stream.events.slice().sort(function (left, right) {
      if (left.event.sequence === right.event.sequence) return left.lineNumber - right.lineNumber;
      return left.event.sequence - right.event.sequence;
    });
    let expected = 1;
    for (const item of events) {
      if (item.event.sequence !== expected) {
        stream.warnings.push(stream.streamId + ": expected sequence " + expected + ", found " + item.event.sequence);
        expected = item.event.sequence;
      }
      expected += 1;
    }
    const starts = events.filter(function (item) { return item.event.kind === "learning_started"; });
    const finishes = events.filter(function (item) { return item.event.kind === "learning_finished"; });
    const failures = events.filter(function (item) { return item.event.kind === "learning_failed"; });
    if (starts.length !== 1) {
      stream.warnings.push(stream.streamId + ": expected one learning_started event, found " + starts.length);
    }
    if (finishes.length > 1 || failures.length > 1) {
      stream.warnings.push(stream.streamId + ": duplicate terminal learning events");
    }
    stream.events = events;
    stream.startedEvent = starts[0] || null;
    stream.finishedEvent = finishes[finishes.length - 1] || null;
    stream.failedEvent = failures[failures.length - 1] || null;
    stream.operationId = stream.startedEvent
      ? stream.startedEvent.event.payload.learning_operation_id
      : stream.streamId.replace(/^learning:/, "");
    const startPayload = stream.startedEvent ? stream.startedEvent.event.payload : null;
    const finishResult = stream.finishedEvent ? stream.finishedEvent.event.payload.result : null;
    stream.triggerRunId = startPayload ? startPayload.trigger_run_id || null : null;
    stream.triggerEpisodeId = startPayload ? startPayload.trigger_episode_id || null : null;
    stream.selectedRunIds = finishResult && Array.isArray(finishResult.selected_run_ids)
      ? finishResult.selected_run_ids.slice()
      : [];
    stream.selectedEvidenceGroupIds = finishResult && Array.isArray(finishResult.selected_evidence_group_ids)
      ? finishResult.selected_evidence_group_ids.slice()
      : [];
    stream.relatedRunIds = stream.triggerRunId ? [stream.triggerRunId] : [];
    return stream;
  }

  function classifyCompanion(value) {
    requireObject(value, "JSON companion must be an object", { code: "unknown_artifact" });
    if (
      value.schema_version === 5 &&
      isObject(value.spec) &&
      Array.isArray(value.attempts) &&
      Array.isArray(value.seed_aggregates)
    ) {
      return "summary";
    }
    if (value.schema_version === 3 && Array.isArray(value.runs)) {
      const hasSpec = value.spec !== null && value.spec !== undefined;
      const hasHash = value.resolved_spec_sha256 !== null && value.resolved_spec_sha256 !== undefined;
      if (hasSpec !== hasHash || (hasSpec && !isObject(value.spec))) {
        throw new ArtifactError("corpus spec and resolved_spec_sha256 must be present together", { code: "invalid_corpus_provenance" });
      }
      return "corpus";
    }
    if (
      value.schema_version === 3 &&
      typeof value.experiment_id === "string" &&
      Array.isArray(value.seeds) &&
      isObject(value.reasoner)
    ) {
      return "spec";
    }
    if (
      typeof value.run_id === "string" &&
      typeof value.started_at === "string" &&
      isObject(value.reasoner)
    ) {
      return "manifest";
    }
    if (
      value.schema_version === 4 &&
      typeof value.comparison_id === "string" &&
      isObject(value.baseline) &&
      isObject(value.candidate)
    ) {
      return "comparison";
    }
    if (
      value.schema_version === 3 &&
      isObject(value.identity) &&
      isObject(value.bundle) &&
      typeof value.source_text === "string"
    ) {
      return "bootstrap";
    }
    throw new ArtifactError("unsupported JSON companion structure", { code: "unknown_artifact" });
  }

  function parseCompanionJson(text, sourceName) {
    const source = sourceName || "artifact.json";
    const value = parseJsonLossless(text, source);
    const kind = classifyCompanion(value);
    const expectedVersion =
      kind === "summary" ? 5
        : kind === "comparison" ? 4
          : kind === "bootstrap" || kind === "spec" ? 3
            : kind === "corpus" ? 3 : null;
    if (expectedVersion !== null && value.schema_version !== expectedVersion) {
      throw new ArtifactError(source + ": unsupported " + kind + " schema version", {
        source: source,
        code: "unsupported_schema",
      });
    }
    return { kind: kind, sourceName: source, value: value, rawText: text };
  }

  function buildSession(trace, companions) {
    if (!trace || trace.kind !== "trace") {
      throw new ArtifactError("a parsed trace is required", { code: "trace_required" });
    }
    const documents = companions || [];
    const singletonByRole = new Map();
    const manifestsByRun = new Map();
    const warnings = trace.warnings.slice();
    for (const document of documents) {
      if (document.kind === "manifest") {
        const runId = document.value.run_id;
        if (manifestsByRun.has(runId)) {
          warnings.push("duplicate manifest for run " + runId + "; first selected file is used");
        } else {
          manifestsByRun.set(runId, document);
        }
      } else if (COMPANION_ROLES.has(document.kind)) {
        if (singletonByRole.has(document.kind)) {
          warnings.push("duplicate " + document.kind + " companion; first selected file is used");
        } else {
          singletonByRole.set(document.kind, document);
        }
      }
    }

    const summaryDocument = singletonByRole.get("summary") || null;
    const corpusDocument = singletonByRole.get("corpus") || null;
    const explicitSpecDocument = singletonByRole.get("spec") || null;
    const comparisonDocument = singletonByRole.get("comparison") || null;
    const bootstrapDocument = singletonByRole.get("bootstrap") || null;
    const summary = summaryDocument ? summaryDocument.value : null;
    const corpus = corpusDocument ? corpusDocument.value : null;
    const explicitSpec = explicitSpecDocument ? explicitSpecDocument.value : null;
    const comparison = comparisonDocument ? comparisonDocument.value : null;
    const bootstrap = bootstrapDocument ? bootstrapDocument.value : null;
    const spec = explicitSpec || (summary && summary.spec) || (corpus && corpus.spec) || null;
    const traceRunIds = new Set(
      trace.runs.map(function (run) {
        return run.runId;
      }),
    );

    for (const runId of manifestsByRun.keys()) {
      if (!traceRunIds.has(runId)) {
        warnings.push("manifest run " + runId + " is absent from trace");
      }
    }
    if (summary) {
      for (const attempt of summary.attempts) {
        if (attempt && attempt.run_id && !traceRunIds.has(attempt.run_id)) {
          warnings.push("summary attempt run " + attempt.run_id + " is absent from trace");
        }
      }
    }
    if (corpus) {
      for (const corpusRun of corpus.runs) {
        if (corpusRun && corpusRun.run_id && !traceRunIds.has(corpusRun.run_id)) {
          warnings.push("corpus run " + corpusRun.run_id + " is absent from trace");
        }
      }
    }
    validateExperimentLinks(summary, corpus, explicitSpec, warnings);

    const runs = trace.runs.map(function (traceRun) {
      const externalManifestDocument = manifestsByRun.get(traceRun.runId) || null;
      const corpusRun = corpus
        ? corpus.runs.find(function (item) {
            return item.run_id === traceRun.runId;
          }) || null
        : null;
      const summaryAttempt = summary
        ? summary.attempts.find(function (item) {
            return item.run_id === traceRun.runId;
          }) || null
        : null;
      const externalManifest = externalManifestDocument ? externalManifestDocument.value : null;
      const corpusManifest = corpusRun && isObject(corpusRun.manifest) ? corpusRun.manifest : null;
      const manifest = externalManifest || corpusManifest || {};
      const learningStreams = trace.learningStreams.filter(function (stream) {
        return stream.relatedRunIds.includes(traceRun.runId);
      });
      const runWarnings = traceRun.warnings.slice();
      compareManifestSources(
        traceRun.runId,
        [
          { label: "selected manifest", value: externalManifest },
          { label: "corpus manifest", value: corpusManifest },
        ],
        runWarnings,
      );
      compareOutcomeSources(traceRun, summaryAttempt, corpusRun, runWarnings);
      return deriveRunView({
        traceRun: traceRun,
        manifest: manifest,
        manifestSource: externalManifest
          ? externalManifestDocument.sourceName
          : corpusManifest
            ? "decision corpus"
            : null,
        summaryAttempt: summaryAttempt,
        corpusRun: corpusRun,
        learningStreams: learningStreams,
        spec: spec,
        warnings: runWarnings,
      });
    });

    return {
      trace: trace,
      runs: runs,
      warnings: uniqueStrings(warnings.concat(runs.flatMap(function (run) { return run.warnings; }))),
      summary: summary,
      summaryDocument: summaryDocument,
      corpus: corpus,
      corpusDocument: corpusDocument,
      spec: spec,
      specDocument: explicitSpecDocument,
      comparison: comparison,
      comparisonDocument: comparisonDocument,
      bootstrap: bootstrap,
      bootstrapDocument: bootstrapDocument,
      learningStreams: trace.learningStreams,
      companions: documents.slice(),
    };
  }

  function validateExperimentLinks(summary, corpus, explicitSpec, warnings) {
    const pairs = [
      ["summary", summary],
      ["corpus", corpus],
      ["resolved spec", explicitSpec],
    ].filter(function (entry) {
      return entry[1];
    });
    for (let leftIndex = 0; leftIndex < pairs.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < pairs.length; rightIndex += 1) {
        const left = pairs[leftIndex];
        const right = pairs[rightIndex];
        const leftSpec = left[1].spec || left[1];
        const rightSpec = right[1].spec || right[1];
        if (
          leftSpec.experiment_id &&
          rightSpec.experiment_id &&
          leftSpec.experiment_id !== rightSpec.experiment_id
        ) {
          warnings.push(
            left[0] + " experiment_id " + leftSpec.experiment_id + " conflicts with " + right[0] + " " + rightSpec.experiment_id,
          );
        }
        const leftHash = left[1].resolved_spec_sha256;
        const rightHash = right[1].resolved_spec_sha256;
        if (leftHash && rightHash && leftHash !== rightHash) {
          warnings.push(left[0] + " resolved spec hash conflicts with " + right[0]);
        }
      }
    }
  }

  function compareManifestSources(runId, sources, warnings) {
    const fields = [
      "run_id",
      "seed",
      "agent_id",
      "agent_version",
      "experiment_id",
      "repeat_index",
      "resolved_spec_sha256",
      "memory_mode",
    ];
    const present = sources.filter(function (source) {
      return source.value;
    });
    for (const field of fields) {
      for (let leftIndex = 0; leftIndex < present.length; leftIndex += 1) {
        for (let rightIndex = leftIndex + 1; rightIndex < present.length; rightIndex += 1) {
          const leftValue = present[leftIndex].value[field];
          const rightValue = present[rightIndex].value[field];
          if (leftValue !== null && leftValue !== undefined && rightValue !== null && rightValue !== undefined && !sameValue(leftValue, rightValue)) {
            warnings.push(
              "run " + runId + ": " + field + " conflicts between " + present[leftIndex].label + " and " + present[rightIndex].label,
            );
          }
        }
      }
    }
  }

  function compareOutcomeSources(traceRun, summaryAttempt, corpusRun, warnings) {
    const traceResult = traceRun.finishedEvent ? finishedResult(traceRun.finishedEvent.event.payload) : null;
    const summaryResult = summaryAttempt && summaryAttempt.result ? summaryAttempt.result : null;
    const corpusResult = corpusRun && corpusRun.final_outcome ? corpusRun.final_outcome : null;
    const fields = ["status", "steps", "final_state"];
    const sources = [
      { label: "trace run_finished", value: traceResult },
      { label: "summary result", value: summaryResult },
      { label: "corpus final outcome", value: corpusResult },
    ].filter(function (source) {
      return source.value;
    });
    for (const field of fields) {
      for (let index = 1; index < sources.length; index += 1) {
        const canonical = sources[0].value[field];
        const candidate = sources[index].value[field];
        if (canonical !== null && canonical !== undefined && candidate !== null && candidate !== undefined && !sameValue(canonical, candidate)) {
          warnings.push(
            "run " + traceRun.runId + ": " + field + " conflicts between " + sources[0].label + " and " + sources[index].label,
          );
        }
      }
    }
  }

  function sameValue(left, right) {
    if (typeof left === "bigint" || typeof right === "bigint") {
      return String(left) === String(right);
    }
    if (isObject(left) || Array.isArray(left) || isObject(right) || Array.isArray(right)) {
      return stableStringify(left) === stableStringify(right);
    }
    return left === right;
  }

  function finishedResult(payload) {
    return isObject(payload.result_details) && Object.keys(payload.result_details).length
      ? payload.result_details : payload.result;
  }

  function deriveRunView(input) {
    const traceRun = input.traceRun;
    const finishedPayload = traceRun.finishedEvent ? traceRun.finishedEvent.event.payload : null;
    const result = finishedPayload ? finishedResult(finishedPayload) : null;
    const failurePayload = traceRun.failedEvent ? traceRun.failedEvent.event.payload : null;
    const failure = failurePayload ? failurePayload.failure : null;
    const summaryResult = input.summaryAttempt && input.summaryAttempt.result ? input.summaryAttempt.result : null;
    const finalOutcome = result || summaryResult || (input.corpusRun && input.corpusRun.final_outcome) || null;
    const rows = traceRun.decisionEvents.map(function (item) {
      return deriveDecisionRow(item);
    });
    attachEpisodeEvents(rows, traceRun.episodeEvents || []);
    linkFollowingAssessments(rows);
    markUnchangedPolls(rows);

    const manifest = input.manifest || {};
    const firstState = traceRun.startedEvent ? traceRun.startedEvent.event.payload.initial_state : null;
    const maxIterations = firstDefined(
      rows.length && rows[0].progress ? rows[0].progress.step_limit : null,
      input.spec ? input.spec.max_iterations : null,
    );
    const steps = firstDefined(finalOutcome ? finalOutcome.steps : null, rows.length);
    const budgetReached =
      isFiniteNumeric(steps) && isFiniteNumeric(maxIterations)
        ? numericCompare(steps, maxIterations) >= 0
        : false;
    const simulatorStatus = firstDefined(
      result ? result.status : null,
      summaryResult ? summaryResult.status : null,
      manifest.status,
    );
    const startPayload = traceRun.startedEvent ? traceRun.startedEvent.event.payload : null;
    const startTime = manifest.started_at || (startPayload && startPayload.started_at) || (traceRun.startedEvent ? traceRun.startedEvent.event.recorded_at : null);
    const startTimeSource = manifest.started_at
      ? input.manifestSource || "manifest"
      : startPayload && startPayload.started_at
        ? "run_started.started_at"
        : traceRun.startedEvent
          ? "run_started.recorded_at"
          : null;
    const terminalEvent = traceRun.finishedEvent || traceRun.failedEvent;
    const endTime = manifest.finished_at || (finishedPayload && finishedPayload.finished_at) || (terminalEvent ? terminalEvent.event.recorded_at : null);
    const endTimeSource = manifest.finished_at
      ? input.manifestSource || "manifest"
      : finishedPayload && finishedPayload.finished_at
        ? "run_finished.finished_at"
        : terminalEvent
          ? terminalEvent.event.kind + ".recorded_at"
          : null;
    const metrics = (finishedPayload && finishedPayload.metrics) || (finalOutcome && finalOutcome.metrics) || manifest.metrics || null;
    const learningOperations = (input.learningStreams || []).map(deriveLearningOperation);

    const runView = {
      runId: traceRun.runId,
      traceRun: traceRun,
      rows: rows,
      manifest: manifest,
      manifestSource: input.manifestSource,
      summaryAttempt: input.summaryAttempt,
      corpusRun: input.corpusRun,
      spec: input.spec,
      result: result,
      finalOutcome: finalOutcome,
      failure: failure,
      warnings: uniqueStrings(input.warnings || []),
      lifecycle: {
        runner: traceRun.finishedEvent ? "finished" : traceRun.failedEvent ? "failed" : "incomplete",
        simulator: simulatorStatus === null || simulatorStatus === undefined ? "unknown" : String(simulatorStatus),
        simulatorCompleted: simulatorStatus === "completed",
        budgetReached: budgetReached,
        terminalDecision: rows.some(function (row) { return row.terminalDecision; }),
        terminalObservation: rows.some(function (row) { return row.terminalObservation; }),
      },
      startTime: startTime,
      startTimeSource: startTimeSource,
      endTime: endTime,
      endTimeSource: endTimeSource,
      maxIterations: maxIterations,
      steps: steps,
      stopReason: finalOutcome ? finalOutcome.stop_reason : null,
      metrics: metrics,
      outcomeMetrics: collectOutcomeMetrics(finalOutcome),
      decisionMap: deriveDecisionMap(rows),
      memoryView: startPayload ? startPayload.memory_view : null,
      environmentId: startPayload ? startPayload.environment_id : null,
      environmentProfileVersion: startPayload ? startPayload.environment_profile_version : null,
      initialState: firstState,
      learningOperations: learningOperations,
    };
    runView.strategyEpisodes = deriveStrategyEpisodes(rows);
    runView.charts = collectChartData(runView);
    runView.timing = deriveTimingSummary(runView);
    return runView;
  }

  function attachEpisodeEvents(rows, episodeEvents) {
    const byStep = new Map();
    for (const row of rows) byStep.set(row.iteration, row);
    for (const item of episodeEvents) {
      const payload = item.event.payload;
      const step = item.event.kind === "episode_closed"
        ? payload.episode && payload.episode.step
        : null;
      let row = isSafeInteger(step) ? byStep.get(step) : null;
      if (!row && item.event.kind === "episode_committed") {
        row = rows.find(function (candidate) {
          return candidate.closedEpisode && candidate.closedEpisode.record_id === payload.episode_id;
        });
      }
      if (!row) continue;
      if (item.event.kind === "episode_closed") {
        row.closedEpisode = payload.episode;
        row.episodeWriteDisposition = payload.write_disposition;
        row.episodeClosedAt = item.event.recorded_at;
      } else {
        row.episodeCommit = payload.commit;
        row.episodeCommittedAt = item.event.recorded_at;
      }
    }
  }

  function deriveLearningOperation(stream) {
    const startItem = stream.startedEvent;
    const finishItem = stream.finishedEvent;
    const failureItem = stream.failedEvent;
    const start = startItem ? startItem.event.payload : null;
    const finish = finishItem ? finishItem.event.payload : null;
    const failure = failureItem ? failureItem.event.payload : null;
    const evaluations = stream.events
      .filter(function (item) { return item.event.kind === "lesson_evaluated"; })
      .map(function (item) {
        return {
          recordedAt: item.event.recorded_at,
          sequence: item.event.sequence,
          batch: item.event.payload.batch,
          proposal: item.event.payload.proposal,
          gateResult: item.event.payload.gate_result,
          failure: item.event.payload.failure,
        };
      });
    const activations = stream.events
      .filter(function (item) { return item.event.kind === "lesson_activated"; })
      .map(function (item) {
        return {
          recordedAt: item.event.recorded_at,
          sequence: item.event.sequence,
          lessonId: item.event.payload.lesson_id,
          commit: item.event.payload.commit,
        };
      });
    const result = finish ? finish.result : null;
    const startedAt = start ? start.started_at || startItem.event.recorded_at : null;
    const finishedAt = finish
      ? finish.finished_at || finishItem.event.recorded_at
      : failureItem
        ? failureItem.event.recorded_at
        : null;
    return {
      streamId: stream.streamId,
      operationId: stream.operationId,
      selectedRunIds: stream.selectedRunIds.slice(),
      triggerRunId: stream.triggerRunId,
      triggerEpisodeId: stream.triggerEpisodeId,
      selectedEvidenceGroupIds: stream.selectedEvidenceGroupIds.slice(),
      trigger: start ? start.trigger : result ? result.trigger : null,
      baseView: start ? start.base_view : result ? result.start_view : null,
      endView: result ? result.end_view : null,
      startedAt: startedAt,
      finishedAt: finishedAt,
      wallDurationSeconds: timestampDeltaSeconds(startedAt, finishedAt),
      evaluations: evaluations,
      activations: activations,
      result: result,
      learnerTelemetry: result ? result.learner_telemetry : null,
      failure: failure ? failure.failure : result ? result.failure : null,
      status: failure ? "failed" : finish ? "finished" : "incomplete",
      warnings: stream.warnings.slice(),
    };
  }

  function deriveTimingSummary(runView) {
    const rowModel = sumKnownDurations(runView.rows.map(function (row) {
      return row.payload.model_duration_seconds;
    }));
    const rowEnvironment = sumKnownDurations(runView.rows.map(function (row) {
      return row.payload.environment_duration_seconds;
    }));
    const retrieval = sumKnownDurations(runView.rows.map(function (row) {
      return row.retrievalDiagnostics ? row.retrievalDiagnostics.duration_seconds : null;
    }));
    const metrics = runView.metrics || {};
    const bootstrapTelemetry = runView.manifest ? runView.manifest.bootstrap_reasoner : null;
    const learnerDurations = runView.learningOperations.map(function (operation) {
      return operation.learnerTelemetry ? operation.learnerTelemetry.duration_seconds : null;
    });
    const learner = sumKnownDurations(learnerDurations);
    const runWall = timestampDeltaSeconds(runView.startTime, runView.endTime);
    const phases = [
      {
        key: "run_wall",
        label: "Run wall clock",
        durationSeconds: runWall,
        note: "run start → terminal run event",
      },
      {
        key: "runner_measured",
        label: "Runner measured total",
        durationSeconds: runView.finalOutcome ? runView.finalOutcome.duration_seconds : null,
        note: "monotonic duration saved in RunResult",
      },
      {
        key: "bootstrap_model",
        label: "Bootstrap model",
        durationSeconds: bootstrapTelemetry
          ? bootstrapTelemetry.duration_seconds
          : runView.summaryAttempt && runView.summaryAttempt.trace_metrics && runView.summaryAttempt.trace_metrics.bootstrap
            ? runView.summaryAttempt.trace_metrics.bootstrap.duration_seconds
            : null,
        note: "environment profile bootstrap; may be cached or outside the operational run",
      },
      {
        key: "decision_model",
        label: "Decision model",
        durationSeconds: firstDefined(metrics.model_duration_seconds, rowModel.value),
        note: rowModel.complete ? "sum of decision traces" : "aggregate may be incomplete",
      },
      {
        key: "memory_retrieval",
        label: "Memory retrieval",
        durationSeconds: retrieval.value,
        note: retrieval.complete ? "sum of recorded recall diagnostics" : "some decisions lack retrieval timing",
      },
      {
        key: "environment",
        label: "Environment",
        durationSeconds: firstDefined(metrics.environment_duration_seconds, rowEnvironment.value),
        note: "includes transport and local adapter work",
      },
      {
        key: "transport",
        label: "Transport subset",
        durationSeconds: metrics.transport_duration_seconds,
        note: "external calls only; already included in Environment",
      },
      {
        key: "learner_model",
        label: "Learner model",
        durationSeconds: learner.value !== null
          ? learner.value
          : runView.summaryAttempt && runView.summaryAttempt.trace_metrics && runView.summaryAttempt.trace_metrics.learner
            ? runView.summaryAttempt.trace_metrics.learner.duration_seconds
            : null,
        note: "linked learning streams; may run after the operational run",
      },
    ];
    return {
      startedAt: runView.startTime,
      finishedAt: runView.endTime,
      phases: phases,
      longestDecision: longestDecisionTiming(runView.rows),
      learningOperationCount: runView.learningOperations.length,
    };
  }

  function sumKnownDurations(values) {
    let total = 0;
    let known = 0;
    for (const value of values) {
      if (typeof value === "number" && Number.isFinite(value)) {
        total += value;
        known += 1;
      }
    }
    return { value: known ? total : null, complete: known === values.length };
  }

  function longestDecisionTiming(rows) {
    let longest = null;
    for (const row of rows) {
      const model = row.payload.model_duration_seconds;
      const environment = row.payload.environment_duration_seconds;
      if (!isFiniteNumeric(model) && !isFiniteNumeric(environment)) continue;
      const total = Number(model || 0) + Number(environment || 0);
      if (!longest || total > longest.totalSeconds) {
        longest = {
          iteration: row.iteration,
          actionName: row.actionName,
          modelSeconds: isFiniteNumeric(model) ? Number(model) : null,
          environmentSeconds: isFiniteNumeric(environment) ? Number(environment) : null,
          totalSeconds: total,
        };
      }
    }
    return longest;
  }

  function linkFollowingAssessments(rows) {
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      const next = rows[index + 1] || null;
      const decisionState = next && isObject(next.agentWorkingState)
        ? next.agentWorkingState
        : null;
      const exactLink = Boolean(
        next &&
        next.previousVerification &&
        decisionState &&
        decisionState.open_decision &&
        decisionState.open_decision.step === row.iteration,
      );
      row.followingAssessment = exactLink
        ? next.previousVerification
        : null;
      row.assessmentIteration = row.followingAssessment ? next.iteration : null;
      row.assessmentLinkStatus = exactLink
        ? "linked"
        : next
          ? "missing_decision_state_link"
          : "run_ended";
    }
  }

  function deriveStrategyEpisodes(rows) {
    const episodes = [];
    let current = null;
    for (const row of rows) {
      const decisionState = isObject(row.agentWorkingState) ? row.agentWorkingState : {};
      const continues =
        typeof row.strategy === "string" &&
        decisionState.active_strategy === row.strategy &&
        isSafeInteger(decisionState.strategy_started_step);
      const startedIteration = continues
        ? decisionState.strategy_started_step
        : row.iteration;
      const episodeId = "strategy-" + startedIteration;
      row.strategyStartedIteration = startedIteration;
      row.strategyEpisodeId = episodeId;
      row.strategyContinues = continues;
      if (!current || current.id !== episodeId || current.strategy !== row.strategy) {
        current = {
          id: episodeId,
          startedIteration: startedIteration,
          endedIteration: row.iteration,
          strategy: row.strategy,
          rows: [],
          phases: [],
          actionCounts: {},
          hasScale: false,
          hasFix: false,
          hasDeployment: false,
          hasIncident: false,
          hasContradiction: false,
        };
        episodes.push(current);
      }
      current.rows.push(row);
      current.endedIteration = row.iteration;
      if (row.phase && !current.phases.includes(row.phase)) current.phases.push(row.phase);
      if (row.actionName) {
        current.actionCounts[row.actionName] = (current.actionCounts[row.actionName] || 0) + 1;
      }
      current.hasScale = current.hasScale || row.groups.has("scaling");
      current.hasFix = current.hasFix || row.groups.has("fixes");
      current.hasDeployment = current.hasDeployment || row.groups.has("deployments");
      current.hasIncident =
        current.hasIncident || row.incidentCodes.length > 0 || row.failedObservation;
      current.hasContradiction =
        current.hasContradiction ||
        Boolean(row.followingAssessment && row.followingAssessment.status === "contradicted");
    }
    for (const episode of episodes) {
      episode.items = foldCadenceRows(episode.rows);
    }
    return episodes;
  }

  function foldCadenceRows(rows) {
    const items = [];
    let index = 0;
    while (index < rows.length) {
      if (!isCadenceCandidate(rows[index])) {
        items.push({ kind: "decision", row: rows[index] });
        index += 1;
        continue;
      }
      let end = index + 1;
      while (
        end < rows.length &&
        isCadenceCandidate(rows[end]) &&
        rows[end].actionName !== rows[end - 1].actionName
      ) {
        end += 1;
      }
      const cadenceRows = rows.slice(index, end);
      const advances = cadenceRows.filter(function (row) {
        return isAdvance(row.actionName);
      });
      const logs = cadenceRows.filter(function (row) {
        return isLogRead(row.actionName);
      });
      if (cadenceRows.length >= 4 && advances.length >= 2 && logs.length >= 2) {
        items.push({
          kind: "cadence",
          startIteration: cadenceRows[0].iteration,
          endIteration: cadenceRows[cadenceRows.length - 1].iteration,
          rows: cadenceRows,
          cycles: advances.length,
          logReads: logs.length,
        });
      } else {
        cadenceRows.forEach(function (row) {
          items.push({ kind: "decision", row: row });
        });
      }
      index = end;
    }
    return items;
  }

  function isCadenceCandidate(row) {
    return (
      (isAdvance(row.actionName) || isLogRead(row.actionName)) &&
      !row.failedObservation &&
      !row.policyRejected &&
      !row.providerOrSchemaFailure &&
      !row.incidentCodes.includes("DDOS_MITIGATION_REQUIRED") &&
      !(row.followingAssessment && row.followingAssessment.status === "contradicted")
    );
  }






  function deriveDecisionMap(rows) {
    const nodes = rows.map(function (row) {
      return {
        iteration: row.iteration,
        evidenceSummary: row.preObservation ? row.preObservation.summary : null,
        currentSituation: envelopeSituation(row.envelope),
        hypothesis: envelopeThought(row.envelope),
        actionName: row.actionName,
        arguments: row.arguments,
        observationSummary: row.observation ? row.observation.summary : null,
        observationOk: row.observation ? row.observation.ok : null,
        policyAccepted: row.policyResult ? row.policyResult.accepted : null,
        terminal: row.terminalDecision || row.terminalObservation,
        pendingOperationCount: Array.isArray(row.decisionView.pending_operations)
          ? row.decisionView.pending_operations.length
          : 0,
        incidentCodes: row.incidentCodes.slice(),
        freeTextIncidentCodes: row.freeTextIncidentCodes.slice(),
        groups: Array.from(row.groups),
        scaleDirection: row.scaleDirection,
        unchangedPoll: row.unchangedPoll,
      };
    });
    const chronology = [];
    for (let index = 1; index < nodes.length; index += 1) {
      chronology.push({
        fromIteration: nodes[index - 1].iteration,
        toIteration: nodes[index].iteration,
        kind: "recorded_sequence",
      });
    }
    return { nodes: nodes, chronology: chronology };
  }

  function deriveDecisionRow(item) {
    const payload = item.event.payload;
    const context = payload.context_projection || {};
    const preState = context.environment_state || {};
    const decisionView = isObject(preState.decision_view) ? preState.decision_view : {};
    const preObservation = isObject(preState.latest_observation) ? preState.latest_observation : null;
    const agentWorkingState = isObject(context.agent_working_state) ? context.agent_working_state : {};
    const memoryBrief = isObject(context.memory_brief) ? context.memory_brief : {};
    const progress = isObject(context.progress) ? context.progress : null;
    const decision = payload.decision || null;
    const envelope = decision && decision.envelope ? decision.envelope : null;
    const action = isObject(payload.action) ? payload.action : decisionAction(envelope);
    const observation = payload.observation || null;
    const preClock = extractClock(preObservation);
    const postClock = extractClock(observation);
    const preTime = preClock ? preClock.simulation_time : null;
    const postTime = postClock ? postClock.simulation_time : null;
    const simulationDeltaSeconds = timestampDeltaSeconds(preTime, postTime);
    const actionName = action && typeof action.name === "string" ? action.name : null;
    const argumentsValue = action && isObject(action.arguments) ? action.arguments : {};
    const requestedAdvance = isAdvance(actionName) ? argumentsValue.duration_seconds : null;
    const appliedAdvance = postClock ? postClock.applied_advance_seconds : null;
    const telemetry = decision && decision.telemetry
      ? decision.telemetry
      : !decision && payload.failure && payload.failure.telemetry
        ? payload.failure.telemetry
        : null;
    const tokenUsage = telemetry && telemetry.token_usage ? telemetry.token_usage : null;
    const incidentCodes = collectIncidentCodes(payload);
    const freeTextIncidentCodes = collectFreeTextIncidentCodes(payload, incidentCodes);
    const policyRejected = Boolean(payload.policy_result && payload.policy_result.accepted === false);
    const failureCategory = payload.failure ? payload.failure.category : null;
    const providerOrSchemaFailure = Boolean(
      payload.failure &&
        (payload.failure.stage === "reasoner" ||
          failureCategory === "provider" ||
          failureCategory === "invalid_output" ||
          failureCategory === "transient"),
    );
    const pendingOperations = Array.isArray(decisionView.pending_operations) ? decisionView.pending_operations : [];
    const activeOperationDuringAdvance = Boolean(
      isAdvance(actionName) &&
        pendingOperations.some(function (operation) {
          return operation && ACTIVE_OPERATION_STATUSES.has(operation.status);
        }),
    );
    const scaleDirection = actionName === "server.create" ? "up"
      : actionName === "server.delete" ? "down" : "unknown";
    const groups = actionGroups(actionName);
    const searchText = [
      payload.iteration,
      actionName,
      stringifyForDisplay(argumentsValue, 0),
      observation ? observation.summary : null,
      envelopeSituation(envelope),
      envelopeThought(envelope),
      envelope ? stringifyForDisplay(envelope.contradicting_evidence, 0) : null,
      envelope ? stringifyForDisplay(envelope.expected_result, 0) : null,
      envelope ? stringifyForDisplay(envelope.verification, 0) : null,
      payload.failure ? payload.failure.message : null,
      incidentCodes.join(" "),
      stringifyForDisplay(memoryBrief, 0),
      payload.retrieval_diagnostics ? stringifyForDisplay(payload.retrieval_diagnostics, 0) : null,
    ]
      .filter(function (value) { return value !== null && value !== undefined; })
      .join(" ")
      .toLocaleLowerCase();
    return {
      iteration: payload.iteration,
      event: item.event,
      payload: payload,
      rawLine: item.rawLine,
      lineNumber: item.lineNumber,
      sequence: item.event.sequence,
      recordedAt: item.event.recorded_at,
      context: context,
      preState: preState,
      decisionView: decisionView,
      preObservation: preObservation,
      agentWorkingState: agentWorkingState,
      memoryBrief: memoryBrief,
      progress: progress,
      capabilities: payload.capabilities || context.capabilities || { items: [] },
      retrievalDiagnostics: payload.retrieval_diagnostics || null,
      decision: decision,
      envelope: envelope,
      phase: envelope && typeof envelope.phase === "string" ? envelope.phase : null,
      facts: envelope && Array.isArray(envelope.facts) ? envelope.facts : [],
      competingHypotheses:
        envelope && Array.isArray(envelope.competing_hypotheses)
          ? envelope.competing_hypotheses
          : [],
      contradictingEvidence:
        envelope && Array.isArray(envelope.contradicting_evidence)
          ? envelope.contradicting_evidence
          : [],
      strategy: envelope && typeof envelope.strategy === "string" ? envelope.strategy : null,
      previousVerification:
        envelope && isObject(envelope.previous_verification)
          ? envelope.previous_verification
          : null,
      expectedResult:
        envelope && Array.isArray(envelope.expected_result) ? envelope.expected_result : [],
      verification: envelope && Array.isArray(envelope.verification) ? envelope.verification : [],
      action: action,
      actionName: actionName,
      arguments: argumentsValue,
      observation: observation,
      policyResult: payload.policy_result || null,
      failure: payload.failure || null,
      telemetry: telemetry,
      tokenUsage: tokenUsage,
      preClock: preClock,
      postClock: postClock,
      preTime: preTime,
      postTime: postTime,
      simulationDeltaSeconds: simulationDeltaSeconds,
      requestedAdvance: requestedAdvance,
      appliedAdvance: appliedAdvance,
      advanceMismatch:
        isFiniteNumeric(requestedAdvance) && isFiniteNumeric(appliedAdvance)
          ? numericCompare(requestedAdvance, appliedAdvance) !== 0
          : false,
      failedObservation: Boolean(observation && observation.ok === false),
      policyRejected: policyRejected,
      providerOrSchemaFailure: providerOrSchemaFailure,
      terminalDecision: Boolean(envelope && envelope.task_completed === true),
      terminalObservation: Boolean(observation && observation.terminal === true),
      incidentCodes: incidentCodes,
      freeTextIncidentCodes: freeTextIncidentCodes,
      structuredIncident: incidentCodes.some(function (code) { return STRUCTURED_INCIDENT_CODES.has(code); }),
      activeOperationDuringAdvance: activeOperationDuringAdvance,
      scaleDirection: scaleDirection,
      groups: groups,
      closedEpisode: null,
      episodeWriteDisposition: null,
      episodeCommit: null,
      unchangedPoll: false,
      searchText: searchText,
    };
  }

  function markUnchangedPolls(rows) {
    const previousByCall = new Map();
    for (const row of rows) {
      const capability = (row.capabilities.items || []).find(function (item) { return item.name === row.actionName; });
      if (!capability || capability.mutates_state !== false || !row.observation) {
        continue;
      }
      const callKey = row.actionName + "\0" + stableStringify(row.arguments);
      const payloadSignature = stableStringify(stripClock(row.observation.data || {}));
      const previous = previousByCall.get(callKey);
      row.unchangedPoll = Boolean(previous && previous.payloadSignature === payloadSignature);
      previousByCall.set(callKey, { payloadSignature: payloadSignature, iteration: row.iteration });
    }
  }

  function isAdvance(name) {
    return name === "advanceTime" || name === "advance_time";
  }

  function isLogRead(name) {
    return name === "getSiteLogs" || name === "get_logs";
  }

  function actionGroups(actionName) {
    const groups = new Set();
    if (isAdvance(actionName)) groups.add("advances");
    if (actionName === "firewall.rules.upsert" || actionName === "disk.cleanup") groups.add("fixes");
    if (actionName === "server.create" || actionName === "server.delete") groups.add("scaling");
    if (actionName === "site.database.set" || actionName === "database.restore") groups.add("deployments");
    if (actionName === "getOperation") groups.add("operations");
    return groups;
  }

  function extractClock(observation) {
    if (!observation || !isObject(observation.data)) return null;
    const clocks = [];
    function visit(data, depth) {
      if (!isObject(data) || depth > 4) return;
      if (isObject(data.clock)) clocks.push(data.clock);
      if (typeof data.simulation_time === "string") clocks.push(data);
      // Composite results preserve full observations or explicitly selected paths.
      for (const item of (Array.isArray(data.items) ? data.items : [])) {
        if (isObject(item.observation)) visit(item.observation.data, depth + 1);
      }
      for (const result of (Array.isArray(data.results) ? data.results : [])) {
        if (!isObject(result.selected)) continue;
        visit(result.selected.data, depth + 1);
        const clock = result.selected["data.clock"];
        if (isObject(clock)) clocks.push(clock);
      }
    }
    visit(observation.data, 0);
    return clocks.filter(function (clock) {
      return typeof clock.simulation_time === "string" && Number.isFinite(Date.parse(clock.simulation_time));
    }).sort(function (left, right) {
      return Date.parse(right.simulation_time) - Date.parse(left.simulation_time);
    })[0] || null;
  }

  function timestampDeltaSeconds(before, after) {
    if (typeof before !== "string" || typeof after !== "string") {
      return null;
    }
    const beforeMillis = Date.parse(before);
    const afterMillis = Date.parse(after);
    if (!Number.isFinite(beforeMillis) || !Number.isFinite(afterMillis)) {
      return null;
    }
    return (afterMillis - beforeMillis) / 1000;
  }

  function collectIncidentCodes(payload) {
    const codes = [];
    const observation = payload.observation;
    if (observation && isObject(observation.data)) {
      addCode(codes, observation.data.code);
      addCode(codes, observation.data.error);
      if (Array.isArray(observation.data.logs)) {
        for (const log of observation.data.logs) {
          if (log) addCode(codes, log.error);
        }
      }
      if (isObject(observation.data.error)) {
        addCode(codes, observation.data.error.error);
      }
    }
    if (payload.failure) {
      addCode(codes, payload.failure.category);
      addCode(codes, payload.failure.error_type);
    }
    return uniqueStrings(codes);
  }

  function collectFreeTextIncidentCodes(payload, structuredCodes) {
    const decision = payload.decision;
    const envelope = decision && decision.envelope ? decision.envelope : null;
    const observation = payload.observation;
    const text = [
      envelopeSituation(envelope),
      envelopeThought(envelope),
      envelope ? stringifyForDisplay(envelope.contradicting_evidence, 0) : null,
      observation ? observation.summary : null,
      observation && observation.data ? stringifyForDisplay(observation.data, 0) : null,
      payload.failure ? payload.failure.message : null,
    ]
      .filter(function (value) { return typeof value === "string"; })
      .join(" ")
      .toLocaleLowerCase();
    return Array.from(STRUCTURED_INCIDENT_CODES).filter(function (code) {
      return !structuredCodes.includes(code) && text.includes(code.toLocaleLowerCase());
    });
  }

  function decisionAction(envelope) {
    if (!isObject(envelope)) return null;
    return isObject(envelope.selected_action) ? envelope.selected_action : null;
  }

  function envelopeSituation(envelope) {
    if (!isObject(envelope)) return null;
    return Array.isArray(envelope.facts) ? envelope.facts.join(" · ") : null;
  }

  function envelopeThought(envelope) {
    if (!isObject(envelope)) return null;
    return typeof envelope.strategy === "string" ? envelope.strategy : null;
  }

  function addCode(codes, value) {
    if (typeof value === "string" && value.length) {
      codes.push(value);
    }
  }


  // Read only published final-state fields; missing values remain missing.
  function collectOutcomeMetrics(outcome) {
    const state = outcome && isObject(outcome.final_state) ? outcome.final_state : {};
    const evaluation = isObject(state.evaluation) ? state.evaluation : {};
    const costs = isObject(state.costs) ? state.costs : {};
    const availability = isObject(state.availability) ? state.availability : {};
    return {
      score: evaluation.score ?? null,
      max_score: evaluation.max_score ?? null,
      total_cost_minor: costs.total_cost_minor ?? null,
      currency: costs.currency ?? null,
      uptime_ratio: availability.uptime_ratio ?? null,
      uptime_target: availability.uptime_target ?? null,
      downtime_seconds: availability.downtime_seconds ?? null,
      slo_passed: availability.slo_passed ?? null,
      site_status: state.site_status ?? null,
    };
  }

  function collectChartData(runView) {
    const rows = runView.rows;
    const simulationTime = [];
    const simulationDelta = [];
    const advances = [];
    const durationSeries = cumulativeSeries(rows, [
      { name: "model", getter: function (row) { return row.payload.model_duration_seconds; } },
      { name: "environment", getter: function (row) { return row.payload.environment_duration_seconds; } },
    ]);
    const tokenSeries = cumulativeSeries(rows, [
      { name: "input", getter: function (row) { return row.tokenUsage ? row.tokenUsage.input_tokens : null; } },
      { name: "output", getter: function (row) { return row.tokenUsage ? row.tokenUsage.output_tokens : null; } },
      { name: "reasoning", getter: function (row) { return row.tokenUsage ? row.tokenUsage.reasoning_output_tokens : null; } },
      { name: "total", getter: function (row) { return row.tokenUsage ? row.tokenUsage.total_tokens : null; } },
    ]);
    const markers = [];
    const actionCounts = new Map();

    for (const row of rows) {
      if (row.postTime) {
        simulationTime.push({ iteration: row.iteration, timestamp: row.postTime, source: row.actionName || "observation" });
      }
      if (row.simulationDeltaSeconds !== null) {
        simulationDelta.push({ iteration: row.iteration, value: row.simulationDeltaSeconds, source: "pre/post clock" });
      }
      if (isAdvance(row.actionName)) {
        advances.push({
          iteration: row.iteration,
          requested: row.requestedAdvance,
          applied: row.appliedAdvance,
          mismatch: row.advanceMismatch,
        });
      }
      if (row.actionName) {
        actionCounts.set(row.actionName, (actionCounts.get(row.actionName) || 0) + 1);
      }
      if (
        row.incidentCodes.length ||
        row.freeTextIncidentCodes.length ||
        row.groups.has("fixes") ||
        row.groups.has("scaling") ||
        row.groups.has("deployments") ||
        row.groups.has("operations")
      ) {
        markers.push({
          iteration: row.iteration,
          action: row.actionName,
          codes: row.incidentCodes,
          freeTextCodes: row.freeTextIncidentCodes,
          groups: Array.from(row.groups),
          scaleDirection: row.scaleDirection,
        });
      }
    }
    return {
      simulationTime: simulationTime,
      budgetHorizon: collectBudgetHorizon(rows, runView.maxIterations),
      simulationDelta: simulationDelta,
      advances: advances,
      durations: durationSeries,
      tokens: tokenSeries,
      markers: markers,
      actionCounts: Array.from(actionCounts.entries())
        .map(function (entry) { return { name: entry[0], count: entry[1] }; })
        .sort(function (left, right) { return right.count - left.count || left.name.localeCompare(right.name); }),
    };
  }

  function collectBudgetHorizon(rows, maxIterations) {
    const budget = [];
    const horizon = [];
    const firstClockRow = rows.find(function (row) { return row.preTime && row.preClock; });
    const horizonStart = firstClockRow ? Date.parse(firstClockRow.preTime) : NaN;
    const horizonEnd =
      firstClockRow && typeof firstClockRow.preClock.simulation_ends_at === "string"
        ? Date.parse(firstClockRow.preClock.simulation_ends_at)
        : NaN;
    const horizonDuration = horizonEnd - horizonStart;
    for (const row of rows) {
      budget.push({
        iteration: row.iteration,
        value:
          isFiniteNumeric(row.iteration) &&
          isFiniteNumeric(maxIterations) &&
          numericCompare(maxIterations, 0) > 0
            ? (Number(row.iteration) / Number(maxIterations)) * 100
            : null,
        source: "iteration / max_iterations",
      });
      const postTime = row.postTime ? Date.parse(row.postTime) : NaN;
      horizon.push({
        iteration: row.iteration,
        value:
          Number.isFinite(postTime) && Number.isFinite(horizonDuration) && horizonDuration > 0
            ? ((postTime - horizonStart) / horizonDuration) * 100
            : null,
        source: "saved simulation timestamps",
      });
    }
    const series = [
      {
        name: "iteration budget used %",
        points: budget,
        incomplete: budget.some(function (point) { return point.value === null; }),
      },
      {
        name: "simulation horizon elapsed %",
        points: horizon,
        incomplete: horizon.some(function (point) { return point.value === null; }),
      },
    ];
    return isFiniteNumeric(maxIterations) ? series : series.slice(1);
  }

  function cumulativeSeries(rows, definitions) {
    const totals = {};
    const incomplete = {};
    const series = {};
    for (const definition of definitions) {
      totals[definition.name] = 0;
      incomplete[definition.name] = false;
      series[definition.name] = [];
    }
    for (const row of rows) {
      for (const definition of definitions) {
        const value = definition.getter(row);
        if (incomplete[definition.name] || !isFiniteNumeric(value)) {
          incomplete[definition.name] = true;
          series[definition.name].push({ iteration: row.iteration, value: null });
          continue;
        }
        const chartValue = toChartNumber(value);
        if (chartValue === null) {
          incomplete[definition.name] = true;
          series[definition.name].push({ iteration: row.iteration, value: null });
          continue;
        }
        totals[definition.name] += chartValue;
        series[definition.name].push({ iteration: row.iteration, value: totals[definition.name] });
      }
    }
    return definitions.map(function (definition) {
      return {
        name: definition.name,
        points: series[definition.name],
        incomplete: incomplete[definition.name],
      };
    });
  }



  function filterDecisionRows(rows, filters) {
    const value = filters || {};
    const search = typeof value.text === "string" ? value.text.trim().toLocaleLowerCase() : "";
    const groups = new Set(value.groups || []);
    return rows.filter(function (row) {
      if (value.action && row.actionName !== value.action) return false;
      if (isFiniteNumeric(value.minIteration) && numericCompare(row.iteration, value.minIteration) < 0) return false;
      if (isFiniteNumeric(value.maxIteration) && numericCompare(row.iteration, value.maxIteration) > 0) return false;
      if (value.failedObservation && !row.failedObservation) return false;
      if (value.policyRejected && !row.policyRejected) return false;
      if (value.providerFailure && !row.providerOrSchemaFailure) return false;
      if (value.terminal && !row.terminalDecision) return false;
      if (
        value.errors &&
        !(row.incidentCodes.length || row.freeTextIncidentCodes.length || row.failedObservation || row.failure)
      ) return false;
      for (const group of groups) {
        if (!row.groups.has(group)) return false;
      }
      if (search && !row.searchText.includes(search)) return false;
      return true;
    });
  }

  function stripClock(value) {
    if (Array.isArray(value)) {
      return value.map(stripClock);
    }
    if (!isObject(value)) {
      return value;
    }
    const result = {};
    for (const key of Object.keys(value)) {
      if (key !== "clock") {
        result[key] = stripClock(value[key]);
      }
    }
    return result;
  }

  function stableStringify(value) {
    if (typeof value === "bigint") return value.toString();
    if (Array.isArray(value)) {
      return "[" + value.map(stableStringify).join(",") + "]";
    }
    if (isObject(value)) {
      return (
        "{" +
        Object.keys(value)
          .sort()
          .map(function (key) { return JSON.stringify(key) + ":" + stableStringify(value[key]); })
          .join(",") +
        "}"
      );
    }
    return JSON.stringify(value);
  }

  function stringifyForDisplay(value, spacing) {
    return JSON.stringify(
      value,
      function (_key, item) {
        return typeof item === "bigint" ? item.toString() : item;
      },
      spacing === undefined ? 2 : spacing,
    );
  }

  function formatMinor(value) {
    if (value === null || value === undefined) {
      return { compact: "missing", raw: "missing", chartValue: null };
    }
    if (typeof value === "bigint") {
      const absolute = value < 0n ? -value : value;
      let compact;
      if (absolute >= 1000000000n) {
        compact = compactBigInt(value, 1000000000n, "B");
      } else if (absolute >= 1000000n) {
        compact = compactBigInt(value, 1000000n, "M");
      } else if (absolute >= 1000n) {
        compact = compactBigInt(value, 1000n, "K");
      } else {
        compact = value.toString();
      }
      return { compact: compact + " minor", raw: value.toString(), chartValue: toChartNumber(value) };
    }
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return { compact: String(value), raw: String(value), chartValue: null };
    }
    return {
      compact: new Intl.NumberFormat("en-US", {
        notation: Math.abs(value) >= 1000 ? "compact" : "standard",
        maximumFractionDigits: 2,
      }).format(value) + " minor",
      raw: String(value),
      chartValue: Number.isSafeInteger(value) || !Number.isInteger(value) ? value : null,
    };
  }

  function compactBigInt(value, divisor, suffix) {
    const negative = value < 0n;
    const absolute = negative ? -value : value;
    const whole = absolute / divisor;
    const remainder = (absolute % divisor) * 100n / divisor;
    const decimals = remainder === 0n ? "" : "." + remainder.toString().padStart(2, "0").replace(/0+$/, "");
    return (negative ? "-" : "") + whole.toString() + decimals + suffix;
  }

  function formatScalar(value) {
    if (value === null || value === undefined) return "missing";
    if (typeof value === "bigint") return value.toString();
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (typeof value === "number") {
      return new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(value);
    }
    return String(value);
  }

  function toChartNumber(value) {
    if (typeof value === "bigint") {
      if (value > SAFE_INTEGER_LIMIT || value < -SAFE_INTEGER_LIMIT) return null;
      return Number(value);
    }
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function isFiniteNumeric(value) {
    return typeof value === "bigint" || (typeof value === "number" && Number.isFinite(value));
  }

  function numericCompare(left, right) {
    if (typeof left === "bigint" || typeof right === "bigint") {
      const leftValue = typeof left === "bigint" ? left : BigInt(left);
      const rightValue = typeof right === "bigint" ? right : BigInt(right);
      return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
    }
    return left < right ? -1 : left > right ? 1 : 0;
  }

  function firstDefined() {
    for (const value of arguments) {
      if (value !== null && value !== undefined) return value;
    }
    return null;
  }

  function uniqueStrings(values) {
    return Array.from(new Set(values.filter(function (value) { return typeof value === "string" && value.length; })));
  }

  return {
    ArtifactError: ArtifactError,
    TRACE_SCHEMA_VERSION: TRACE_SCHEMA_VERSION,
    parseJsonLossless: parseJsonLossless,
    parseTraceJsonl: parseTraceJsonl,
    parseCompanionJson: parseCompanionJson,
    classifyCompanion: classifyCompanion,
    buildSession: buildSession,
    deriveDecisionRow: deriveDecisionRow,
    deriveDecisionMap: deriveDecisionMap,
    deriveStrategyEpisodes: deriveStrategyEpisodes,
    collectOutcomeMetrics: collectOutcomeMetrics,
    foldCadenceRows: foldCadenceRows,
    deriveRunView: deriveRunView,
    collectChartData: collectChartData,
    filterDecisionRows: filterDecisionRows,
    formatMinor: formatMinor,
    formatScalar: formatScalar,
    stringifyForDisplay: stringifyForDisplay,
    stableStringify: stableStringify,
    stripClock: stripClock,
    toChartNumber: toChartNumber,
    supportsSourceContext: supportsSourceContext,
  };
});
