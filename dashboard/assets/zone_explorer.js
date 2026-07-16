/* FlowMind Zone Explorer: real SUMO geometry with collision-free static lane slots. */
(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const DEFAULT_ENDPOINT = "/api/zone-explorer";
  const LIVE_REFRESH_MS = 3_000;
  const STATIC_LAYOUT_MAX_VEHICLES = 250;
  const LOAD_COLORS = {
    free: "#62d48b",
    busy: "#f2bf5e",
    critical: "#ff6f61",
  };
  const SIGNAL_LABELS = {
    green: "зелений",
    yellow: "жовтий",
    red: "червоний",
  };
  const MODE_LABELS = {
    static_fixed: "Static Fixed",
    sumo_actuated: "SUMO Actuated",
    local: "Local Adaptive",
    flowmind: "FlowMind",
    fixed: "Fixed (legacy)",
  };

  const asNumber = (value, fallback = null) => {
    if (value === null || value === undefined || value === "") return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };
  const clamp = (value, minimum, maximum) => (
    Math.max(minimum, Math.min(maximum, value))
  );
  const asObject = (value) => (
    value && typeof value === "object" && !Array.isArray(value) ? value : {}
  );
  const asArray = (value) => {
    if (Array.isArray(value)) return value;
    if (Array.isArray(value?.items)) return value.items;
    if (Array.isArray(value?.samples)) return value.samples;
    return [];
  };
  const firstValue = (...values) => values.find((value) => value !== null && value !== undefined && value !== "");
  const truthy = (value) => {
    if (typeof value === "boolean") return value;
    return ["1", "true", "yes", "on"].includes(String(value || "").toLowerCase());
  };
  const validPoint = (value) => (
    Array.isArray(value)
    && value.length >= 2
    && asNumber(value[0]) !== null
    && asNumber(value[1]) !== null
  );
  const validPosition = (value) => (
    asNumber(value?.x) !== null && asNumber(value?.y) !== null
  );
  const escapeSelector = (value) => {
    const text = String(value);
    return window.CSS?.escape ? window.CSS.escape(text) : text.replace(/["\\]/g, "\\$&");
  };
  const round = (value, digits = 1) => {
    const number = asNumber(value);
    if (number === null) return null;
    const factor = 10 ** digits;
    return Math.round(number * factor) / factor;
  };
  const format = (value, suffix = "", digits = 1) => {
    const number = asNumber(value);
    if (number === null) return "—";
    const rendered = Math.abs(number - Math.round(number)) < .001
      ? String(Math.round(number))
      : number.toFixed(digits);
    return `${rendered}${suffix}`;
  };
  const formatTime = (value) => {
    const seconds = Math.max(0, Math.round(asNumber(value, 0)));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  };
  const normaliseSignal = (value) => {
    const state = String(value || "").toLowerCase();
    if (state.includes("y")) return "yellow";
    if (state.includes("g")) return "green";
    return "red";
  };
  const titleCase = (value) => String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
  const shortText = (value, maximum = 90) => {
    const text = String(value ?? "");
    return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
  };

  const element = (tag, className = "", text = null) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== null) node.textContent = String(text);
    return node;
  };
  const svgElement = (tag, attributes = {}) => {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes).forEach(([name, value]) => {
      if (value !== null && value !== undefined) node.setAttribute(name, String(value));
    });
    return node;
  };
  const appendTitle = (node, text) => {
    const title = svgElement("title");
    title.textContent = String(text);
    node.appendChild(title);
  };

  const corridorRows = (corridors) => {
    if (Array.isArray(corridors)) {
      return corridors.map((item, index) => ({
        id: String(item?.corridor_id || item?.id || item?.name || `corridor-${index + 1}`),
        label: String(item?.label || item?.name || item?.corridor_id || item?.id || `Corridor ${index + 1}`),
        tlsIds: asArray(item?.tls_sequence || item?.tls_ids || item?.intersections)
          .map((value) => String(value?.tls_id || value)),
      })).filter((item) => item.tlsIds.length > 1);
    }
    return Object.entries(asObject(corridors)).map(([id, raw]) => ({
      id,
      label: titleCase(id),
      tlsIds: asArray(raw?.tls_sequence || raw?.tls_ids || raw)
        .map((value) => String(value?.tls_id || value)),
    })).filter((item) => item.tlsIds.length > 1);
  };

  const normaliseAdvantages = (value) => {
    if (Array.isArray(value)) return value;
    const source = asObject(value);
    if (Array.isArray(source.items)) return source.items;
    const rows = [];
    Object.entries(source).forEach(([key, raw]) => {
      if (raw && typeof raw === "object" && !Array.isArray(raw)) {
        const item = asObject(raw);
        const metricValue = firstValue(
          item.value,
          item.delta_percent,
          item.improvement_percent,
          item.delta,
          item.average,
        );
        if (metricValue !== undefined) {
          rows.push({ key, ...item, value: metricValue });
        } else {
          Object.entries(item).forEach(([childKey, childValue]) => {
            if (childValue === null || typeof childValue !== "object") {
              rows.push({
                key: `${key}.${childKey}`,
                label: `${titleCase(key)} · ${titleCase(childKey)}`,
                value: childValue,
              });
            }
          });
        }
      } else {
        rows.push({ key, label: titleCase(key), value: raw });
      }
    });
    return rows;
  };

  const normalisePayload = (rawPayload) => {
    const raw = asObject(rawPayload?.payload || rawPayload);
    const source = asObject(raw.source);
    const zone = asObject(raw.zone);
    const fallbackScene = asObject(raw.zone_simulation);
    const scene = {
      ...fallbackScene,
      ...asObject(raw.scene),
    };
    const definitions = asArray(zone.intersections);
    const definitionsById = new Map(
      definitions.map((item) => [String(item?.tls_id || item?.id || ""), asObject(item)]),
    );
    const intersections = asArray(scene.intersections || raw.intersections)
      .filter(validPosition)
      .map((item, index) => {
        const tlsId = String(item?.tls_id || item?.id || `tls-${index + 1}`);
        const definition = definitionsById.get(tlsId) || {};
        return {
          ...definition,
          ...item,
          tls_id: tlsId,
          name: String(item?.name || definition.name || definition.label || tlsId),
          short_label: String(item?.short_label || definition.short_label || `I-${String(index + 1).padStart(2, "0")}`),
          x: Number(item.x),
          y: Number(item.y),
          incoming_queue: Math.max(0, asNumber(item.incoming_queue, 0)),
          incoming_vehicles: Math.max(0, asNumber(item.incoming_vehicles, 0)),
          outgoing_occupancy: clamp(asNumber(item.outgoing_occupancy, 0), 0, 1),
          signal: normaliseSignal(item.signal || item.state),
          movements: asArray(item.movements),
        };
      });
    const lanes = asArray(scene.lanes || raw.lanes).map((item) => ({
      ...item,
      lane_id: String(item?.lane_id || item?.id || ""),
      shape: asArray(item?.shape).filter(validPoint).map((point) => [
        Number(point[0]),
        Number(point[1]),
      ]),
      directions: asArray(item?.directions).map((value) => String(value).toLowerCase()),
      tls_ids: asArray(item?.tls_ids).map(String),
      vehicle_count: Math.max(0, asNumber(item?.vehicle_count, 0)),
      queue: Math.max(0, asNumber(item?.queue, 0)),
      occupancy: clamp(asNumber(item?.occupancy, 0), 0, 1),
      mean_speed: Math.max(0, asNumber(item?.mean_speed, 0)),
    })).filter((item) => item.lane_id && item.shape.length >= 2);
    const vehicles = asArray(scene.vehicles || raw.vehicles)
      .filter(validPosition)
      .map((item, index) => ({
        ...item,
        vehicle_id: String(item?.vehicle_id || item?.id || `vehicle-${index + 1}`),
        lane_id: String(item?.lane_id || ""),
        x: Number(item.x),
        y: Number(item.y),
        speed: Math.max(0, asNumber(item?.speed, 0)),
        angle: asNumber(item?.angle, 0),
        is_priority: truthy(item?.is_priority || item?.priority),
      }));
    const timeline = asObject(raw.timeline);
    const samples = asArray(timeline.samples || raw.metric_history)
      .filter((item) => item && typeof item === "object")
      .sort((left, right) => asNumber(left.time, 0) - asNumber(right.time, 0));
    const sourceStatus = String(
      source.status
      || scene.status
      || raw.system?.simulation?.status
      || "unknown",
    ).toLowerCase();
    const sourceKind = String(source.kind || (sourceStatus === "running" ? "live" : "archive")).toLowerCase();
    const sceneIsStatic = truthy(
      source.scene_is_static
      || timeline.positions_are_static
      || sourceKind === "archive"
      || sourceStatus === "completed",
    );
    const illustrative = truthy(
      source.illustrative
      || sourceKind === "static"
    );
    return {
      available: raw.available !== false && Boolean(intersections.length || lanes.length),
      source: {
        ...source,
        kind: sourceKind,
        status: sourceStatus,
        mode: String(source.mode || scene.mode || raw.mode || raw.summary?.mode || ""),
        result_id: source.result_id || raw.id || null,
        snapshot_time: firstValue(source.snapshot_time, scene.simulated_time, raw.simulated_time),
        scene_is_static: sceneIsStatic,
        illustrative,
      },
      zone: {
        ...zone,
        id: String(zone.id || zone.zone_id || "flowmind-zone"),
        name: String(zone.name || "FlowMind control zone"),
        intersections: definitions,
        corridors: corridorRows(zone.corridors),
      },
      scene: { intersections, lanes, vehicles },
      timeline: {
        ...timeline,
        samples,
        positions_are_static: sceneIsStatic,
      },
      actions: asArray(raw.actions || raw.decision_log),
      predictions: asArray(raw.predictions || raw.queue_forecast || timeline.predictions),
      summary: asObject(raw.summary),
      traffic_flow: asObject(raw.traffic_flow),
      system: asObject(raw.system),
      advantages: normaliseAdvantages(raw.advantages || raw.comparison?.advantages),
      comparison: asObject(raw.comparison),
      raw,
    };
  };

  const loadSeverity = (vehicles, queue, occupancy) => {
    const vehicleRatio = clamp(asNumber(vehicles, 0) / 10, 0, 1);
    const queueRatio = clamp(asNumber(queue, 0) / 10, 0, 1);
    const occupancyRatio = clamp(asNumber(occupancy, 0), 0, 1);
    return Math.max(vehicleRatio, queueRatio, occupancyRatio);
  };
  const severityLevel = (severity) => (
    severity >= .75 ? "critical" : severity >= .4 ? "busy" : "free"
  );

  const polylineMetrics = (shape, project = (x, y) => ({ x, y })) => {
    const points = asArray(shape).filter(validPoint).map(([x, y]) => project(x, y));
    const segments = [];
    let totalLength = 0;
    points.slice(0, -1).forEach((start, index) => {
      const end = points[index + 1];
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      const length = Math.hypot(dx, dy);
      if (length <= 1e-6) return;
      segments.push({
        start,
        end,
        dx,
        dy,
        length,
        offset: totalLength,
      });
      totalLength += length;
    });
    return { points, segments, totalLength };
  };

  const samplePolyline = (metrics, distance) => {
    if (!metrics?.segments?.length || metrics.totalLength <= 0) return null;
    const target = clamp(asNumber(distance, 0), 0, metrics.totalLength);
    const segment = metrics.segments.find(
      (item) => target <= item.offset + item.length,
    ) || metrics.segments.at(-1);
    const ratio = clamp((target - segment.offset) / segment.length, 0, 1);
    return {
      x: segment.start.x + segment.dx * ratio,
      y: segment.start.y + segment.dy * ratio,
      angle: Math.atan2(segment.dy, segment.dx) * 180 / Math.PI,
      distance: target,
    };
  };

  const pointSegmentDistanceSquared = (point, start, end) => {
    const dx = end[0] - start[0];
    const dy = end[1] - start[1];
    const denominator = dx * dx + dy * dy;
    if (denominator <= 1e-9) {
      return (point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2;
    }
    const ratio = clamp(
      ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator,
      0,
      1,
    );
    const projectedX = start[0] + dx * ratio;
    const projectedY = start[1] + dy * ratio;
    return (point[0] - projectedX) ** 2 + (point[1] - projectedY) ** 2;
  };

  const nearestLaneId = (vehicle, lanes, maximumDistance = 42) => {
    if (!validPosition(vehicle)) return null;
    const point = [Number(vehicle.x), Number(vehicle.y)];
    let bestLaneId = null;
    let bestDistanceSquared = maximumDistance ** 2;
    [...lanes].sort((left, right) => left.lane_id.localeCompare(right.lane_id))
      .forEach((lane) => {
        lane.shape.slice(0, -1).forEach((start, index) => {
          const distanceSquared = pointSegmentDistanceSquared(
            point,
            start,
            lane.shape[index + 1],
          );
          if (distanceSquared < bestDistanceSquared - 1e-9) {
            bestDistanceSquared = distanceSquared;
            bestLaneId = lane.lane_id;
          }
        });
      });
    return bestLaneId;
  };

  const orientedBox = (x, y, angle, length, width) => {
    const radians = angle * Math.PI / 180;
    const longitudinal = { x: Math.cos(radians), y: Math.sin(radians) };
    const lateral = { x: -longitudinal.y, y: longitudinal.x };
    return {
      x,
      y,
      axes: [longitudinal, lateral],
      halfLength: length / 2,
      halfWidth: width / 2,
    };
  };

  const boxesOverlap = (left, right) => {
    const delta = { x: right.x - left.x, y: right.y - left.y };
    return [...left.axes, ...right.axes].every((axis) => {
      const centerDistance = Math.abs(delta.x * axis.x + delta.y * axis.y);
      const leftRadius = (
        Math.abs(left.axes[0].x * axis.x + left.axes[0].y * axis.y) * left.halfLength
        + Math.abs(left.axes[1].x * axis.x + left.axes[1].y * axis.y) * left.halfWidth
      );
      const rightRadius = (
        Math.abs(right.axes[0].x * axis.x + right.axes[0].y * axis.y) * right.halfLength
        + Math.abs(right.axes[1].x * axis.x + right.axes[1].y * axis.y) * right.halfWidth
      );
      return centerDistance < leftRadius + rightRadius - 1e-7;
    });
  };

  const buildStaticVehicleLayout = (lanes, vehicles, projection, options = {}) => {
    const maximumVehicles = Math.max(
      0,
      Math.round(asNumber(options.maximumVehicles, STATIC_LAYOUT_MAX_VEHICLES)),
    );
    const laneById = new Map(lanes.map((lane) => [lane.lane_id, lane]));
    const assignedByLane = new Map(lanes.map((lane) => [lane.lane_id, []]));
    let reassignedCount = 0;
    let unassignedCount = 0;

    vehicles.forEach((vehicle) => {
      let lane = laneById.get(vehicle.lane_id);
      if (!lane) {
        const nearestId = nearestLaneId(
          vehicle,
          lanes,
          asNumber(options.maximumSnapDistance, 42),
        );
        lane = nearestId ? laneById.get(nearestId) : null;
        if (lane) reassignedCount += 1;
      }
      if (!lane) {
        unassignedCount += 1;
        return;
      }
      assignedByLane.get(lane.lane_id).push({
        ...vehicle,
        original_lane_id: vehicle.lane_id,
        lane_id: lane.lane_id,
      });
    });

    const laneRows = lanes.map((lane) => {
      const assigned = assignedByLane.get(lane.lane_id)
        .sort((left, right) => (
          Number(Boolean(right.is_priority)) - Number(Boolean(left.is_priority))
          || left.vehicle_id.localeCompare(right.vehicle_id)
        ));
      const telemetryCount = Math.max(0, Math.round(asNumber(lane.vehicle_count, 0)));
      return {
        lane,
        assigned,
        targetCount: Math.max(telemetryCount, assigned.length),
        severity: loadSeverity(lane.vehicle_count, lane.queue, lane.occupancy),
      };
    }).filter((row) => row.targetCount > 0)
      .sort((left, right) => (
        right.severity - left.severity
        || left.lane.lane_id.localeCompare(right.lane.lane_id)
      ));

    const vehicleLength = asNumber(projection.vehicleLength, 7);
    const vehicleBreadth = asNumber(projection.vehicleBreadth, 2.4);
    const markerRadius = asNumber(projection.markerRadius, 0);
    const spacing = Math.max(
      vehicleLength * 1.25,
      asNumber(options.minimumSpacing, 0),
    );
    const collisionLength = vehicleLength + Math.max(1, vehicleLength * .14);
    const collisionBreadth = vehicleBreadth + Math.max(.45, vehicleBreadth * .18);
    const occupiedBoxes = [];
    const placements = [];
    const requestedCount = laneRows.reduce(
      (sum, row) => sum + row.targetCount,
      0,
    );
    let omittedCount = unassignedCount;

    laneRows.forEach(({ lane, assigned, targetCount }) => {
      if (placements.length >= maximumVehicles) {
        omittedCount += targetCount;
        return;
      }
      const metrics = polylineMetrics(lane.shape, projection.project);
      const leadMargin = Math.min(
        markerRadius + vehicleLength * .62,
        Math.max(vehicleLength * .8, metrics.totalLength * .32),
      );
      const tailMargin = Math.min(
        vehicleLength * .62,
        metrics.totalLength * .15,
      );
      const availableLength = metrics.totalLength - leadMargin - tailMargin;
      if (availableLength < 0) {
        omittedCount += targetCount;
        return;
      }
      const maximumSlots = Math.floor(availableLength / spacing) + 1;
      const laneTarget = Math.min(
        targetCount,
        maximumVehicles - placements.length,
      );
      omittedCount += targetCount - laneTarget;
      const queueFromEnd = lane.directions.includes("incoming")
        || !lane.directions.includes("outgoing");
      const items = assigned.slice(0, laneTarget);
      while (items.length < laneTarget) {
        const slotNumber = items.length + 1;
        items.push({
          vehicle_id: `${lane.lane_id}:static-slot:${slotNumber}`,
          lane_id: lane.lane_id,
          speed: 0,
          is_priority: false,
          synthetic: true,
        });
      }

      let candidateSlot = 0;
      items.forEach((vehicle) => {
        let placement = null;
        while (candidateSlot < maximumSlots && !placement) {
          const distance = queueFromEnd
            ? metrics.totalLength - leadMargin - candidateSlot * spacing
            : leadMargin + candidateSlot * spacing;
          candidateSlot += 1;
          const point = samplePolyline(metrics, distance);
          if (!point) continue;
          const collisionBox = orientedBox(
            point.x,
            point.y,
            point.angle,
            collisionLength,
            collisionBreadth,
          );
          if (occupiedBoxes.some((box) => boxesOverlap(box, collisionBox))) continue;
          placement = {
            ...vehicle,
            lane_id: lane.lane_id,
            x: point.x,
            y: point.y,
            angle: point.angle,
            lane_distance: point.distance,
            queue_from_end: queueFromEnd,
            collision_box: collisionBox,
          };
          occupiedBoxes.push(collisionBox);
          placements.push(placement);
        }
        if (!placement) omittedCount += 1;
      });
    });

    return {
      placements,
      stats: {
        sourceCount: vehicles.length,
        requestedCount,
        placedCount: placements.length,
        omittedCount,
        reassignedCount,
        unassignedCount,
      },
    };
  };

  class ZoneExplorer {
    constructor(root, options = {}) {
      this.root = root;
      if (!this.root.id) this.root.id = "zoneExplorerRoot";
      this.options = options;
      this.endpoint = options.endpoint || root.dataset.endpoint || DEFAULT_ENDPOINT;
      this.payload = null;
      this.selectedTlsId = null;
      this.sampleIndex = 0;
      this.playTimer = null;
      this.refreshTimer = null;
      this.loading = false;
      this.userSelectedTime = false;
      this.baseViewBox = null;
      this.viewBox = null;
      this.projection = null;
      this.laneNodes = new Map();
      this.vehicleNodes = new Map();
      this.vehiclePlacements = [];
      this.vehicleLayoutStats = {
        sourceCount: 0,
        requestedCount: 0,
        placedCount: 0,
        omittedCount: 0,
        reassignedCount: 0,
        unassignedCount: 0,
      };
      this.tlsNodes = new Map();
      this.pointerState = null;
      this.dragDistance = 0;
      this.buildShell();
      this.root.dataset.staticLayout = "true";
      this.bindEvents();
    }

    buildShell() {
      this.root.classList.add("zone-explorer");
      this.root.dataset.illustrative = "false";
      this.root.innerHTML = `
        <div class="zx-shell">
          <header class="zx-toolbar">
            <div class="zx-heading">
              <span class="zx-eyebrow">AREA-AWARE CONTROL · SUMO GEOMETRY</span>
              <h1 data-zx="title">Zone Explorer</h1>
              <p data-zx="subtitle">Виберіть всю зону або окреме перехрестя, щоб побачити потік, рішення та прогноз.</p>
            </div>
            <div class="zx-toolbar-actions">
              <div class="zx-source-badges" id="sourceBadge" data-zx="source-badges"></div>
              <select class="zx-select" id="zoneSelector" data-zx="selection" aria-label="Область аналізу">
                <option value="">Вся зона</option>
              </select>
              <button class="zx-button" type="button" data-zx="zone">Вся зона</button>
              <button class="zx-button" type="button" data-zx="reset-view">Вписати карту</button>
              <button class="zx-button zx-button--icon" type="button" data-zx="refresh" aria-label="Оновити дані" title="Оновити дані">↻</button>
            </div>
          </header>
          <div class="zx-illustrative" id="mapNotice" data-zx="illustrative">
            <strong data-zx="static-title">Статичні черги на смугах</strong>
            <span data-zx="static-detail">Автомобілі стоять у фіксованих lane-слотах без анімації та накладань; їхній колір відповідає завантаженості ділянки.</span>
          </div>
          <div class="zx-layout">
            <section class="zx-map-panel" id="zoneViewport" aria-label="Карта контрольованої зони">
              <div class="zx-map-overlay" data-zx="map-overlay"></div>
              <svg class="zx-map-svg" id="zoneMapSvg" data-zx="map" role="img" tabindex="0" aria-label="Карта SUMO з перехрестями, смугами й автомобілями"></svg>
              <div class="zx-map-legend" aria-label="Легенда навантаження">
                <span><i class="free"></i> вільно</span>
                <span><i class="busy"></i> середнє</span>
                <span><i class="critical"></i> високе</span>
                <span><i class="priority"></i> спецтранспорт</span>
                <span><i class="corridor"></i> коридор зони</span>
              </div>
            </section>
            <aside class="zx-inspector" id="zoneInspector" data-zx="inspector" aria-live="polite"></aside>
          </div>
          <section class="zx-timeline" data-zx="timeline">
            <div class="zx-timeline-actions">
              <button class="zx-button zx-button--icon" type="button" data-zx="previous" aria-label="Попередній зріз">‹</button>
              <button class="zx-button zx-button--icon" id="playButton" type="button" data-zx="play" aria-label="Відтворити">▶</button>
              <button class="zx-button zx-button--icon" type="button" data-zx="next" aria-label="Наступний зріз">›</button>
            </div>
            <div class="zx-timeline-main">
              <div class="zx-timeline-labels">
                <strong id="timelineLabel" data-zx="time-label">00:00</strong>
                <span data-zx="timeline-count">немає часових зрізів</span>
              </div>
              <input class="zx-range" id="timelineRange" data-zx="range" type="range" min="0" max="0" step="1" value="0" disabled aria-label="Час симуляції">
            </div>
            <div class="zx-timeline-note" data-zx="timeline-note">Очікуємо дані.</div>
          </section>
        </div>
      `;
      this.map = this.query("map");
      this.inspector = this.query("inspector");
    }

    query(name) {
      return this.root.querySelector(`[data-zx="${name}"]`);
    }

    bindEvents() {
      this.query("selection").addEventListener("change", (event) => {
        this.selectTls(event.target.value || null);
      });
      this.query("zone").addEventListener("click", () => this.selectTls(null));
      this.query("reset-view").addEventListener("click", () => this.resetView());
      this.query("refresh").addEventListener("click", () => this.load());
      this.query("previous").addEventListener("click", () => this.stepTimeline(-1));
      this.query("next").addEventListener("click", () => this.stepTimeline(1));
      this.query("play").addEventListener("click", () => this.togglePlayback());
      this.query("range").addEventListener("input", (event) => {
        this.userSelectedTime = true;
        this.sampleIndex = clamp(
          Math.round(asNumber(event.target.value, 0)),
          0,
          Math.max(this.samples.length - 1, 0),
        );
        this.renderDynamic();
      });
      this.map.addEventListener("wheel", (event) => this.handleWheel(event), { passive: false });
      this.map.addEventListener("pointerdown", (event) => this.handlePointerDown(event));
      this.map.addEventListener("pointermove", (event) => this.handlePointerMove(event));
      this.map.addEventListener("pointerup", (event) => this.handlePointerUp(event));
      this.map.addEventListener("pointercancel", (event) => this.handlePointerUp(event));
      this.map.addEventListener("dblclick", () => this.resetView());
      this.map.addEventListener("keydown", (event) => {
        if (event.key === "0") this.resetView();
        if (event.key === "+" || event.key === "=") this.zoomAt(.8);
        if (event.key === "-") this.zoomAt(1.25);
      });
    }

    get samples() {
      return this.payload?.timeline?.samples || [];
    }

    get currentSample() {
      if (!this.samples.length) return {};
      return this.samples[clamp(this.sampleIndex, 0, this.samples.length - 1)] || {};
    }

    get currentTime() {
      return firstValue(
        this.currentSample.time,
        this.payload?.source?.snapshot_time,
        this.payload?.summary?.simulated_duration,
        0,
      );
    }

    async load(options = {}) {
      if (this.loading) return;
      this.loading = true;
      this.query("refresh").disabled = true;
      if (!options.quiet) this.setLoading();
      try {
        const url = new URL(this.endpoint, window.location.href);
        if (!url.searchParams.has("result_id")) {
          const resultId = new URLSearchParams(window.location.search).get("result_id");
          if (resultId) url.searchParams.set("result_id", resultId);
        }
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) throw new Error(await response.text() || response.statusText);
        const wasLatest = !this.samples.length || this.sampleIndex >= this.samples.length - 1;
        this.setPayload(await response.json(), {
          preserveSelection: options.quiet,
          preserveTime: options.quiet && !wasLatest,
        });
      } catch (error) {
        this.setError(error);
      } finally {
        this.loading = false;
        this.query("refresh").disabled = false;
      }
    }

    setLoading() {
      this.stopPlayback();
      this.inspector.innerHTML = `
        <div class="zx-empty"><div><strong>Завантажуємо Zone Explorer</strong><span>Читаємо геометрію SUMO, телеметрію та журнал рішень.</span></div></div>
      `;
    }

    setError(error) {
      this.stopPlayback();
      this.inspector.innerHTML = "";
      const empty = element("div", "zx-empty zx-error");
      const content = element("div");
      content.append(
        element("strong", "", "Zone Explorer недоступний"),
        element("span", "", shortText(error?.message || error || "Невідома помилка", 220)),
      );
      empty.appendChild(content);
      this.inspector.appendChild(empty);
      this.query("source-badges").replaceChildren(this.tag("API error", "bad"));
    }

    setPayload(rawPayload, options = {}) {
      const previousTlsId = this.selectedTlsId;
      const previousTime = this.currentTime;
      this.payload = normalisePayload(rawPayload);
      if (!this.payload.available) {
        this.setError(
          this.payload.raw?.error === "unknown result_id"
            ? "Вибраний результат не знайдено в архіві."
            : "Payload не містить геометрії контрольованої зони.",
        );
        return;
      }
      const knownTls = new Set(this.payload.scene.intersections.map((item) => item.tls_id));
      this.selectedTlsId = options.preserveSelection && knownTls.has(previousTlsId)
        ? previousTlsId
        : null;
      if (options.preserveTime && this.samples.length) {
        this.sampleIndex = this.nearestSampleIndex(previousTime);
      } else {
        this.sampleIndex = Math.max(this.samples.length - 1, 0);
      }
      this.populateSelection();
      this.updateHeader();
      this.buildMap();
      this.resetView();
      this.renderDynamic();
      this.configureLiveRefresh();
    }

    populateSelection() {
      const select = this.query("selection");
      select.replaceChildren(new Option("Вся зона", ""));
      this.payload.scene.intersections.forEach((intersection) => {
        const label = `${intersection.short_label} · ${intersection.name}`;
        select.appendChild(new Option(label, intersection.tls_id));
      });
      select.value = this.selectedTlsId || "";
    }

    updateHeader() {
      const { source, zone, scene } = this.payload;
      this.query("title").textContent = zone.name || "Zone Explorer";
      this.query("subtitle").textContent = (
        `${scene.intersections.length} перехресть · ${scene.lanes.length} контрольованих смуг · `
        + (
          source.illustrative
            ? "traffic snapshot відсутній. "
            : `${scene.vehicles.length} авто у telemetry; карта показує їх статично вздовж смуг. `
        )
        + "Виберіть вузол для пояснення керування."
      );
      const badges = this.query("source-badges");
      badges.replaceChildren(
        this.tag(MODE_LABELS[source.mode] || source.mode || "режим —", source.mode === "flowmind" ? "info" : ""),
        this.tag(`${source.kind || "source"} · ${source.status || "unknown"}`, source.status === "running" ? "good" : "warn"),
        this.tag(
          source.illustrative
            ? "network-only view"
            : "static lane slots",
          "warn",
        ),
      );
      this.root.dataset.illustrative = String(Boolean(source.illustrative));
      this.query("static-title").textContent = source.illustrative
        ? "Статична network-only сцена"
        : "Статичні черги на смугах";
      this.query("static-detail").textContent = source.illustrative
        ? "Показана реальна геометрія мережі без traffic snapshot; автомобілі не вигадуються."
        : "Автомобілі розкладені один за одним у фіксованих слотах уздовж реальної SUMO-геометрії смуг. Спрайти не рухаються й не перекриваються; колір відповідає lane load.";
    }

    tag(text, kind = "") {
      return element("span", `zx-tag${kind ? ` zx-tag--${kind}` : ""}`, text);
    }

    buildMap() {
      this.map.replaceChildren();
      this.laneNodes.clear();
      this.vehicleNodes.clear();
      this.vehiclePlacements = [];
      this.tlsNodes.clear();
      const points = [];
      this.payload.scene.lanes.forEach((lane) => lane.shape.forEach((point) => points.push(point)));
      this.payload.scene.intersections.forEach((item) => points.push([item.x, item.y]));
      if (!points.length) return;

      const xs = points.map((point) => point[0]);
      const ys = points.map((point) => point[1]);
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);
      const rawWidth = Math.max(maxX - minX, 100);
      const rawHeight = Math.max(maxY - minY, 100);
      const extent = Math.max(rawWidth, rawHeight);
      const padding = clamp(extent * .045, 35, 220);
      const width = rawWidth + padding * 2;
      const height = rawHeight + padding * 2;
      this.projection = {
        extent,
        markerRadius: clamp(extent * .0065, 11, 38),
        vehicleLength: clamp(extent * .0024, 6.5, 10),
        vehicleBreadth: clamp(extent * .0006, 1.9, 2.4),
        project: (x, y) => ({
          x: Number(x) - minX + padding,
          y: maxY - Number(y) + padding,
        }),
      };
      this.baseViewBox = { x: 0, y: 0, width, height };
      this.viewBox = { ...this.baseViewBox };
      this.map.setAttribute("viewBox", `0 0 ${width} ${height}`);

      const background = svgElement("rect", {
        class: "zx-map-background",
        x: 0,
        y: 0,
        width,
        height,
      });
      background.addEventListener("click", () => {
        if (this.dragDistance < 5) this.selectTls(null);
      });
      this.map.appendChild(background);

      const intersectionsById = new Map(
        this.payload.scene.intersections.map((item) => [item.tls_id, item]),
      );
      const corridorLayer = svgElement("g", { class: "zx-corridors" });
      this.payload.zone.corridors.forEach((corridor) => {
        corridor.tlsIds.slice(0, -1).forEach((tlsId, index) => {
          const left = intersectionsById.get(tlsId);
          const right = intersectionsById.get(corridor.tlsIds[index + 1]);
          if (!left || !right) return;
          const a = this.projection.project(left.x, left.y);
          const b = this.projection.project(right.x, right.y);
          const line = svgElement("line", {
            class: "zx-corridor-link",
            x1: a.x,
            y1: a.y,
            x2: b.x,
            y2: b.y,
            "stroke-width": 3,
            "data-corridor-id": corridor.id,
          });
          appendTitle(line, corridor.label);
          corridorLayer.appendChild(line);
        });
      });
      this.map.appendChild(corridorLayer);

      const laneLayer = svgElement("g", { class: "zx-lanes" });
      this.payload.scene.lanes.forEach((lane) => {
        const pointsText = lane.shape.map(([x, y]) => {
          const point = this.projection.project(x, y);
          return `${point.x},${point.y}`;
        }).join(" ");
        const polyline = svgElement("polyline", {
          class: "zx-lane",
          points: pointsText,
          "stroke-width": 2.3,
          "data-lane-id": lane.lane_id,
        });
        appendTitle(
          polyline,
          `${lane.lane_id} · ${lane.vehicle_count} авто · q ${lane.queue} · occupancy ${format(lane.occupancy * 100, "%")}`,
        );
        polyline.addEventListener("click", (event) => {
          event.stopPropagation();
          if (this.dragDistance >= 5) return;
          const tlsId = lane.tls_ids.find((id) => intersectionsById.has(id));
          if (tlsId) this.selectTls(tlsId);
        });
        laneLayer.appendChild(polyline);
        this.laneNodes.set(lane.lane_id, polyline);
      });
      this.map.appendChild(laneLayer);

      const layout = buildStaticVehicleLayout(
        this.payload.scene.lanes,
        this.payload.scene.vehicles,
        this.projection,
      );
      this.vehiclePlacements = layout.placements;
      this.vehicleLayoutStats = layout.stats;
      const vehicleLayer = svgElement("g", { class: "zx-vehicles" });
      this.vehiclePlacements.forEach((vehicle) => {
        const lengthValue = this.projection.vehicleLength;
        const breadthValue = this.projection.vehicleBreadth;
        const group = svgElement("g", {
          class: `zx-vehicle${vehicle.is_priority ? " is-priority" : ""}`,
          transform: `translate(${vehicle.x} ${vehicle.y}) rotate(${vehicle.angle})`,
          "data-vehicle-id": vehicle.vehicle_id,
          "data-lane-id": vehicle.lane_id,
          "data-static-slot": "true",
        });
        group.append(
          svgElement("rect", {
            class: "zx-vehicle-body",
            x: -lengthValue * .5,
            y: -breadthValue / 2,
            width: lengthValue * .82,
            height: breadthValue,
            rx: breadthValue * .25,
            "stroke-width": 1,
          }),
          svgElement("path", {
            class: "zx-vehicle-body",
            d: `M ${lengthValue * .32} ${-breadthValue / 2} L ${lengthValue * .5} 0 L ${lengthValue * .32} ${breadthValue / 2} Z`,
            "stroke-width": 1,
          }),
        );
        appendTitle(
          group,
          `${vehicle.synthetic ? "Статичний lane-slot" : vehicle.vehicle_id} · ${vehicle.lane_id || "lane —"} · позиція схематична`,
        );
        vehicleLayer.appendChild(group);
        this.vehicleNodes.set(vehicle.vehicle_id, group);
      });
      this.map.appendChild(vehicleLayer);

      const tlsLayer = svgElement("g", { class: "zx-intersections" });
      this.payload.scene.intersections.forEach((intersection) => {
        const point = this.projection.project(intersection.x, intersection.y);
        const radius = this.projection.markerRadius;
        const group = svgElement("g", {
          class: "zx-tls",
          transform: `translate(${point.x} ${point.y})`,
          tabindex: "0",
          role: "button",
          "data-tls-id": intersection.tls_id,
          "aria-label": `${intersection.short_label}: ${intersection.name}`,
        });
        group.append(
          svgElement("circle", {
            class: "zx-tls-ring",
            r: radius,
            "stroke-width": 2.3,
          }),
          svgElement("circle", {
            class: "zx-tls-signal",
            r: radius * .34,
          }),
        );
        const label = svgElement("text", {
          class: "zx-tls-label",
          y: -radius * 1.35,
          "font-size": clamp(radius * .72, 9, 21),
          "stroke-width": clamp(radius * .2, 2, 5),
        });
        label.textContent = intersection.short_label;
        group.appendChild(label);
        appendTitle(
          group,
          `${intersection.name}\n${intersection.tls_id}\nФаза ${intersection.phase ?? "—"} · q ${intersection.incoming_queue}`,
        );
        group.addEventListener("click", (event) => {
          event.stopPropagation();
          if (this.dragDistance < 5) this.selectTls(intersection.tls_id);
        });
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            this.selectTls(intersection.tls_id);
          }
        });
        tlsLayer.appendChild(group);
        this.tlsNodes.set(intersection.tls_id, group);
      });
      this.map.appendChild(tlsLayer);
    }

    nearestSampleIndex(targetTime) {
      if (!this.samples.length) return 0;
      const target = asNumber(targetTime, asNumber(this.samples.at(-1)?.time, 0));
      let bestIndex = 0;
      let bestDistance = Infinity;
      this.samples.forEach((sample, index) => {
        const distance = Math.abs(asNumber(sample.time, 0) - target);
        if (distance < bestDistance) {
          bestDistance = distance;
          bestIndex = index;
        }
      });
      return bestIndex;
    }

    globalCongestionSeverity() {
      if (!this.samples.length) return 0;
      const sample = this.currentSample;
      const maximum = (key, floor = 1) => Math.max(
        floor,
        ...this.samples.map((row) => asNumber(row?.[key], 0)),
      );
      const queue = clamp(
        asNumber(sample.queue_length, 0) / maximum("queue_length", 1),
        0,
        1,
      );
      const wait = clamp(
        asNumber(sample.waiting_time, 0) / maximum("waiting_time", 1),
        0,
        1,
      );
      const blocked = clamp(
        asNumber(sample.blocked_outgoing_share, 0)
          / maximum("blocked_outgoing_share", .05),
        0,
        1,
      );
      const outflowKey = sample.zone_outflow_per_minute !== undefined
        ? "zone_outflow_per_minute"
        : "outflow_per_minute";
      const maxOutflow = maximum(outflowKey, 1);
      const outflowDeficit = clamp(
        1 - asNumber(sample[outflowKey], maxOutflow) / maxOutflow,
        0,
        1,
      );
      return clamp(queue * .36 + wait * .24 + blocked * .28 + outflowDeficit * .12, 0, 1);
    }

    relatedLaneIds(tlsId) {
      if (!tlsId) return new Set(this.payload.scene.lanes.map((lane) => lane.lane_id));
      const intersection = this.payload.scene.intersections.find((item) => item.tls_id === tlsId);
      const movementLanes = new Set(
        asArray(intersection?.movements)
          .flatMap((movement) => [movement?.incoming_lane, movement?.outgoing_lane])
          .filter(Boolean)
          .map(String),
      );
      this.payload.scene.lanes.forEach((lane) => {
        if (lane.tls_ids.includes(tlsId)) movementLanes.add(lane.lane_id);
      });
      return movementLanes;
    }

    renderDynamic() {
      if (!this.payload) return;
      this.applyMapState();
      this.renderMapOverlay();
      this.renderInspector();
      this.renderTimeline();
    }

    applyMapState() {
      const selectedLanes = this.relatedLaneIds(this.selectedTlsId);
      const lanesById = new Map(this.payload.scene.lanes.map((lane) => [lane.lane_id, lane]));
      this.payload.scene.lanes.forEach((lane) => {
        const severity = loadSeverity(lane.vehicle_count, lane.queue, lane.occupancy);
        const level = severityLevel(severity);
        const node = this.laneNodes.get(lane.lane_id);
        if (!node) return;
        node.dataset.load = level;
        const related = !this.selectedTlsId || selectedLanes.has(lane.lane_id);
        node.classList.toggle("is-related", Boolean(this.selectedTlsId && related));
        node.classList.toggle("is-dimmed", Boolean(this.selectedTlsId && !related));
      });

      this.vehiclePlacements.forEach((vehicle) => {
        const node = this.vehicleNodes.get(vehicle.vehicle_id);
        if (!node) return;
        const lane = lanesById.get(vehicle.lane_id);
        const severity = lane
          ? loadSeverity(lane.vehicle_count, lane.queue, lane.occupancy)
          : 0;
        const level = severityLevel(severity);
        node.style.setProperty("--car-color", vehicle.is_priority ? "#66b7ff" : LOAD_COLORS[level]);
        node.dataset.load = level;
        node.classList.toggle(
          "is-dimmed",
          Boolean(this.selectedTlsId && !selectedLanes.has(vehicle.lane_id)),
        );
      });

      this.payload.scene.intersections.forEach((intersection) => {
        const node = this.tlsNodes.get(intersection.tls_id);
        if (!node) return;
        const baseSeverity = loadSeverity(
          intersection.incoming_vehicles,
          intersection.incoming_queue,
          intersection.outgoing_occupancy,
        );
        node.dataset.load = severityLevel(baseSeverity);
        node.dataset.signal = intersection.signal;
        node.classList.toggle("is-selected", intersection.tls_id === this.selectedTlsId);
        node.classList.toggle(
          "is-dimmed",
          Boolean(this.selectedTlsId && intersection.tls_id !== this.selectedTlsId),
        );
      });

      const relatedCorridors = new Set(
        this.payload.zone.corridors
          .filter((corridor) => corridor.tlsIds.includes(this.selectedTlsId))
          .map((corridor) => corridor.id),
      );
      this.map.querySelectorAll(".zx-corridor-link").forEach((node) => {
        node.classList.toggle(
          "is-related",
          Boolean(this.selectedTlsId && relatedCorridors.has(node.dataset.corridorId)),
        );
      });
      this.query("selection").value = this.selectedTlsId || "";
      this.query("zone").classList.toggle("zx-button--active", !this.selectedTlsId);
    }

    renderMapOverlay() {
      const overlay = this.query("map-overlay");
      overlay.replaceChildren(
        this.tag(`SUMO ${formatTime(this.currentTime)}`, "info"),
        this.tag(`${this.payload.scene.intersections.length} TLS`, "good"),
        this.tag(`${this.vehicleLayoutStats.placedCount} статичних авто`, ""),
        this.tag(
          this.payload.source.illustrative
            ? "network-only"
            : this.vehicleLayoutStats.omittedCount
              ? `${this.vehicleLayoutStats.omittedCount} не вмістилося`
              : "без накладань",
          this.vehicleLayoutStats.omittedCount ? "warn" : "good",
        ),
      );
    }

    selectTls(tlsId) {
      const known = this.payload?.scene?.intersections?.some((item) => item.tls_id === tlsId);
      this.selectedTlsId = known ? tlsId : null;
      this.renderDynamic();
      if (this.selectedTlsId) this.focusTls(this.selectedTlsId);
    }

    renderInspector() {
      this.inspector.replaceChildren();
      const intersection = this.selectedTlsId
        ? this.payload.scene.intersections.find((item) => item.tls_id === this.selectedTlsId)
        : null;
      if (intersection) this.renderTlsInspector(intersection);
      else this.renderZoneInspector();
    }

    inspectorHeader(title, subtitle, signal = null) {
      const header = element("header", "zx-inspector-header");
      const titleRow = element("div", "zx-inspector-title");
      titleRow.appendChild(element("h2", "", title));
      if (signal) titleRow.appendChild(this.tag(SIGNAL_LABELS[signal] || signal, signal === "green" ? "good" : signal === "yellow" ? "warn" : "bad"));
      header.append(titleRow, element("p", "zx-inspector-subtitle", subtitle));
      this.inspector.appendChild(header);
      const body = element("div", "zx-inspector-body");
      this.inspector.appendChild(body);
      return body;
    }

    addKpis(container, rows) {
      const grid = element("div", "zx-kpis");
      if (!container.querySelector("#zoneKpis")) grid.id = "zoneKpis";
      rows.forEach((row) => {
        const card = element("div", `zx-kpi${row.kind ? ` zx-kpi--${row.kind}` : ""}`);
        card.append(
          element("span", "", row.label),
          element("strong", "", row.value),
        );
        if (row.note) card.appendChild(element("small", "", row.note));
        grid.appendChild(card);
      });
      container.appendChild(grid);
    }

    addSection(container, title, content, count = null) {
      const section = element("section", "zx-section");
      const heading = element("div", "zx-section-heading");
      heading.append(
        element("h3", "", title),
        element("span", "", count === null ? "" : String(count)),
      );
      section.append(heading, content);
      container.appendChild(section);
      return section;
    }

    renderZoneInspector() {
      const { summary, traffic_flow: trafficFlow, system } = this.payload;
      const sample = this.currentSample;
      const flow = asObject(trafficFlow);
      const outflowRate = firstValue(
        sample.zone_outflow_per_minute,
        sample.outflow_per_minute,
        flow.outflow_per_minute,
      );
      const throughput = firstValue(
        sample.zone_outflow,
        sample.throughput,
        summary.zone_outflow,
        summary.throughput,
      );
      const inflow = firstValue(sample.zone_inflow, summary.zone_inflow);
      const outflow = firstValue(sample.zone_outflow, summary.zone_outflow);
      const netFlow = firstValue(
        inflow !== undefined && outflow !== undefined
          ? asNumber(inflow, 0) - asNumber(outflow, 0)
          : null,
        summary.zone_net_flow,
      );
      const body = this.inspectorHeader(
        this.payload.zone.name,
        `${this.payload.scene.intersections.length} вузлів · ${this.payload.zone.corridors.length} коридорів · зріз ${formatTime(this.currentTime)}`,
      );
      this.addKpis(body, [
        { label: "Outflow / хв", value: format(outflowRate, " авто", 1), kind: "good", note: "межа контрольованої зони" },
        { label: "Пропуск", value: format(throughput, " авто", 0), kind: "info", note: "накопичувальний результат" },
        { label: "Сумарна черга", value: format(firstValue(sample.queue_length, summary.average_queue_length), " авто"), kind: "warn" },
        { label: "Blocked outgoing", value: format(firstValue(sample.blocked_outgoing_share, summary.blocked_outgoing_share) * 100, "%"), kind: "bad" },
        { label: "Середня швидкість", value: format(sample.mean_speed, " м/с") },
        { label: "Net flow", value: format(netFlow, " авто", 0), note: "inflow − outflow" },
      ]);

      const controller = asObject(system.controller);
      const graph = asObject(system.area_graph);
      const corridor = asObject(system.corridor);
      this.addKpis(body, [
        {
          label: "Zone overrides",
          value: format(firstValue(controller.zone_coordination_overrides, summary.zone_coordination_overrides), "", 0),
          kind: "info",
          note: "рішень, де зона змінила локальний вибір",
        },
        {
          label: "Coordination gain",
          value: format(firstValue(controller.zone_coordination_gain_total, summary.zone_coordination_gain_total), "", 2),
          kind: "good",
          note: "сумарний приріст joint objective",
        },
        {
          label: "Graph segments",
          value: format(graph.segments, "", 0),
          note: `${format(graph.monitored_lanes, "", 0)} monitored lanes`,
        },
        {
          label: "Emergency corridor",
          value: corridor.corridor_state || "—",
          kind: corridor.corridor_state === "GREEN_WINDOW" ? "good" : "",
          note: corridor.corridor_active_tls || "немає активного TLS",
        },
      ]);

      const corridors = element("div", "zx-corridor-chips");
      this.payload.zone.corridors.forEach((corridor) => {
        corridors.appendChild(element(
          "span",
          "zx-corridor-chip",
          `${corridor.label} · ${corridor.tlsIds.length} TLS`,
        ));
      });
      this.addSection(body, "Коридори зони", corridors, this.payload.zone.corridors.length);
      this.renderAdvantages(body);
      this.renderActions(body, null);
      this.renderPredictions(body, null);
    }

    renderTlsInspector(intersection) {
      const lanes = this.payload.scene.lanes.filter((lane) => (
        lane.tls_ids.includes(intersection.tls_id)
        || intersection.movements.some((movement) => (
          String(movement?.incoming_lane || "") === lane.lane_id
          || String(movement?.outgoing_lane || "") === lane.lane_id
        ))
      ));
      const laneOccupancy = lanes.length
        ? lanes.reduce((sum, lane) => sum + lane.occupancy, 0) / lanes.length
        : intersection.outgoing_occupancy;
      const relatedCorridors = this.payload.zone.corridors.filter(
        (corridor) => corridor.tlsIds.includes(intersection.tls_id),
      );
      const body = this.inspectorHeader(
        `${intersection.short_label} · ${intersection.name}`,
        `${intersection.tls_id} · ${
          this.payload.source.illustrative
            ? "network default, не telemetry"
            : `snapshot ${formatTime(this.payload.source.snapshot_time)}${this.payload.timeline.positions_are_static ? " · фаза статична" : ""}`
        }`,
        intersection.signal,
      );
      this.addKpis(body, [
        {
          label: "Фаза",
          value: `${intersection.phase ?? "—"}`,
          kind: intersection.signal === "green" ? "good" : intersection.signal === "yellow" ? "warn" : "bad",
          note: `${format(intersection.phase_elapsed, " с")} у поточній фазі`,
        },
        {
          label: "Авто на вході",
          value: format(intersection.incoming_vehicles, " авто", 0),
          kind: "info",
        },
        {
          label: "Черга",
          value: format(intersection.incoming_queue, " авто", 0),
          kind: intersection.incoming_queue >= 10 ? "bad" : intersection.incoming_queue >= 5 ? "warn" : "good",
        },
        {
          label: "Occupancy",
          value: format(laneOccupancy * 100, "%"),
          kind: laneOccupancy >= .75 ? "bad" : laneOccupancy >= .4 ? "warn" : "good",
          note: `${lanes.length} пов’язаних смуг`,
        },
        {
          label: "Phase timing",
          value: `${format(intersection.phase_min_duration, " с")} / ${format(intersection.phase_duration, " с")} / ${format(intersection.phase_max_duration, " с")}`,
          note: "min / default / max",
        },
        {
          label: "Signal state",
          value: shortText(intersection.state || "—", 18),
          note: `program ${intersection.program_id || "—"} · ${intersection.program_type || "—"}`,
        },
      ]);

      const chips = element("div", "zx-corridor-chips");
      relatedCorridors.forEach((corridor) => chips.appendChild(
        element("span", "zx-corridor-chip", corridor.label),
      ));
      this.addSection(body, "Пов’язані коридори", chips, relatedCorridors.length);

      const movementWrap = element("div");
      if (intersection.movements.length) {
        const table = element("table", "zx-movements");
        const head = element("thead");
        const headRow = element("tr");
        ["Сигнал", "Incoming lane", "Outgoing lane"].forEach((label) => {
          headRow.appendChild(element("th", "", label));
        });
        head.appendChild(headRow);
        const tableBody = element("tbody");
        intersection.movements.slice(0, 28).forEach((movement) => {
          const row = element("tr");
          const signalCell = element("td");
          const dot = element("i", "zx-signal-dot");
          dot.dataset.state = String(movement?.state || "r");
          signalCell.append(dot, document.createTextNode(String(movement?.state || "r")));
          row.append(
            signalCell,
            element("td", "", movement?.incoming_lane || "—"),
            element("td", "", movement?.outgoing_lane || "—"),
          );
          tableBody.appendChild(row);
        });
        table.append(head, tableBody);
        movementWrap.appendChild(table);
      } else {
        movementWrap.appendChild(element("div", "zx-list-empty", "Movement telemetry відсутня."));
      }
      this.addSection(body, "Рухи світлофора", movementWrap, intersection.movements.length);
      this.renderActions(body, intersection.tls_id);
      this.renderPredictions(body, intersection.tls_id);
    }

    rowsAtCurrentTime(rows, tlsId = null) {
      const time = asNumber(this.currentTime, Infinity);
      return rows.filter((row) => {
        const matchesTls = !tlsId || String(row?.tls_id || "") === tlsId;
        const rowTime = asNumber(row?.time);
        return matchesTls && (rowTime === null || rowTime <= time + 1e-9);
      });
    }

    renderActions(container, tlsId) {
      const rows = this.rowsAtCurrentTime(this.payload.actions, tlsId).slice(-7).reverse();
      const list = element("div", "zx-list");
      list.id = "actionList";
      if (!rows.length) {
        list.appendChild(element("div", "zx-list-empty", "До цього зрізу рішень не записано."));
      } else {
        rows.forEach((row) => {
          const event = element("article", "zx-event");
          event.dataset.level = String(row?.level || "info").toLowerCase();
          const head = element("div", "zx-event-head");
          head.append(
            element("strong", "", row?.title || row?.category || "Рішення контролера"),
            element("time", "", formatTime(row?.time)),
          );
          event.append(head, element("p", "", row?.detail || row?.tls_id || "Без пояснення."));
          list.appendChild(event);
        });
      }
      this.addSection(container, "Дії контролера", list, rows.length);
    }

    renderPredictions(container, tlsId) {
      const rows = this.rowsAtCurrentTime(this.payload.predictions, tlsId).slice(-6).reverse();
      const list = element("div", "zx-list");
      list.id = "predictionList";
      if (!rows.length) {
        list.appendChild(element("div", "zx-list-empty", "До цього зрізу прогнозів немає або ML вимкнено."));
      } else {
        rows.forEach((row) => {
          const prediction = element("article", "zx-prediction");
          prediction.dataset.ood = String(truthy(row?.ood));
          const head = element("div", "zx-prediction-head");
          head.append(
            element("strong", "", tlsId ? `Прогноз для ${format(row?.horizon_seconds, " с", 0)}` : shortText(row?.tls_name || row?.tls_id || "Zone forecast", 48)),
            element("time", "", formatTime(row?.time)),
          );
          const value = firstValue(row?.mean_prediction, row?.prediction, row?.value);
          prediction.append(
            head,
            element(
              "p",
              "",
              `mean ${format(value, "", 2)} · max ${format(row?.max_prediction, "", 2)} · candidates ${format(row?.candidates, "", 0)}`,
            ),
          );
          const badges = element("div", "zx-prediction-badges");
          badges.appendChild(element(
            "span",
            `zx-mini-badge ${truthy(row?.ood) ? "zx-mini-badge--warn" : "zx-mini-badge--good"}`,
            truthy(row?.ood) ? "OOD" : "in-domain",
          ));
          badges.appendChild(element(
            "span",
            `zx-mini-badge ${truthy(row?.used_for_control) ? "zx-mini-badge--good" : ""}`,
            truthy(row?.used_for_control) ? "used for control" : truthy(row?.shadow) ? "shadow" : "diagnostic",
          ));
          if (asNumber(row?.confidence) !== null) {
            badges.appendChild(element("span", "zx-mini-badge", `confidence ${format(row.confidence * 100, "%", 0)}`));
          }
          if (asNumber(row?.mean_absolute_error) !== null) {
            badges.appendChild(element("span", "zx-mini-badge", `MAE ${format(row.mean_absolute_error, "", 2)}`));
          }
          prediction.appendChild(badges);
          if (row?.diagnostic_reasons) {
            prediction.appendChild(element("p", "", shortText(row.diagnostic_reasons, 150)));
          }
          list.appendChild(prediction);
        });
      }
      this.addSection(container, "Передбачення", list, rows.length);
    }

    renderAdvantages(container) {
      if (!this.payload.advantages.length) return;
      const list = element("div", "zx-list");
      list.id = "advantageList";
      const comparison = this.payload.comparison;
      const evaluation = comparison.evaluation_id
        ? `evaluation ${comparison.evaluation_id}`
        : comparison.scope === "paired_evaluation"
          ? "paired evaluation"
          : "доступний агрегат";
      const pairCount = asNumber(comparison.pair_count);
      list.appendChild(element(
        "div",
        "zx-list-empty",
        `${evaluation}${pairCount === null ? "" : ` · ${format(pairCount, " paired seeds", 0)}`}. Порівняння є benchmark-level і не приписується вибраному snapshot.`,
      ));
      this.payload.advantages.slice(0, 8).forEach((raw, index) => {
        const item = asObject(raw);
        const card = element("article", "zx-advantage");
        const lower = String(item.status || item.kind || "").toLowerCase();
        const delta = asNumber(firstValue(
          item.delta_percent,
          item.improvement_percent,
          item.difference_percent,
        ));
        const kind = lower.includes("negative") || lower.includes("worse")
          ? "negative"
          : lower.includes("warn") || lower.includes("neutral")
            ? ""
            : lower.includes("positive") || lower.includes("better") || (delta !== null && delta > 0)
              ? "positive"
              : "";
        card.dataset.kind = kind;
        const head = element("div", "zx-advantage-head");
        const value = firstValue(item.display_value, item.value, item.delta, item.average);
        const suffix = item.unit || (item.delta_percent !== undefined || item.improvement_percent !== undefined ? "%" : "");
        head.append(
          element("strong", "", item.label || titleCase(item.key || `metric ${index + 1}`)),
          element("span", "zx-advantage-value", typeof value === "number" ? format(value, suffix, 1) : String(value ?? "—")),
        );
        card.append(
          head,
          element("p", "", item.note || item.description || item.scope || "Метрика з актуального benchmark."),
        );
        list.appendChild(card);
      });
      this.addSection(container, "FlowMind проти Local · benchmark", list, this.payload.advantages.length);
    }

    renderTimeline() {
      const range = this.query("range");
      const hasSamples = this.samples.length > 0;
      range.disabled = !hasSamples;
      range.max = String(Math.max(this.samples.length - 1, 0));
      range.value = String(clamp(this.sampleIndex, 0, Math.max(this.samples.length - 1, 0)));
      this.query("time-label").textContent = formatTime(this.currentTime);
      this.query("timeline-count").textContent = hasSamples
        ? `зріз ${this.sampleIndex + 1} / ${this.samples.length}`
        : "збережено лише фінальний snapshot";
      this.query("previous").disabled = !hasSamples || this.sampleIndex <= 0;
      this.query("next").disabled = !hasSamples || this.sampleIndex >= this.samples.length - 1;
      this.query("play").disabled = this.samples.length < 2;
      this.query("timeline-note").textContent = this.payload.source.illustrative
        ? "Traffic snapshot відсутній: показана реальна геометрія мережі та наявні агреговані метрики."
        : "Машини стоять у фіксованих lane-слотах уздовж доріг; колір авто й смуги відповідає останньому lane load. Timeline змінює лише метрики, дії та прогнози.";
    }

    stepTimeline(delta) {
      if (!this.samples.length) return;
      this.userSelectedTime = true;
      this.sampleIndex = clamp(this.sampleIndex + delta, 0, this.samples.length - 1);
      this.renderDynamic();
      if (this.sampleIndex >= this.samples.length - 1 && delta > 0) this.stopPlayback();
    }

    togglePlayback() {
      if (this.playTimer) {
        this.stopPlayback();
        return;
      }
      if (this.samples.length < 2) return;
      if (this.sampleIndex >= this.samples.length - 1) this.sampleIndex = 0;
      this.query("play").textContent = "❚❚";
      this.query("play").setAttribute("aria-label", "Пауза");
      this.playTimer = window.setInterval(() => {
        if (this.sampleIndex >= this.samples.length - 1) {
          this.stopPlayback();
          return;
        }
        this.sampleIndex += 1;
        this.renderDynamic();
      }, 700);
    }

    stopPlayback() {
      if (this.playTimer) window.clearInterval(this.playTimer);
      this.playTimer = null;
      const button = this.query("play");
      if (button) {
        button.textContent = "▶";
        button.setAttribute("aria-label", "Відтворити");
      }
    }

    configureLiveRefresh() {
      if (this.refreshTimer) window.clearInterval(this.refreshTimer);
      this.refreshTimer = null;
      if (
        this.payload.source.kind === "live"
        && this.payload.source.status === "running"
        && this.options.poll === true
      ) {
        this.refreshTimer = window.setInterval(() => this.load({ quiet: true }), LIVE_REFRESH_MS);
      }
    }

    resetView() {
      if (!this.baseViewBox) return;
      this.viewBox = { ...this.baseViewBox };
      this.updateViewBox();
    }

    focusTls(tlsId) {
      const intersection = this.payload.scene.intersections.find((item) => item.tls_id === tlsId);
      if (!intersection || !this.projection || !this.baseViewBox) return;
      const point = this.projection.project(intersection.x, intersection.y);
      const width = this.baseViewBox.width * .34;
      const height = this.baseViewBox.height * .34;
      this.viewBox = {
        x: point.x - width / 2,
        y: point.y - height / 2,
        width,
        height,
      };
      this.updateViewBox();
    }

    zoomAt(factor, clientX = null, clientY = null) {
      if (!this.viewBox || !this.baseViewBox) return;
      const rect = this.map.getBoundingClientRect();
      const sx = clientX === null ? .5 : clamp((clientX - rect.left) / rect.width, 0, 1);
      const sy = clientY === null ? .5 : clamp((clientY - rect.top) / rect.height, 0, 1);
      const minimumWidth = this.baseViewBox.width / 14;
      const maximumWidth = this.baseViewBox.width * 1.35;
      const nextWidth = clamp(this.viewBox.width * factor, minimumWidth, maximumWidth);
      const aspect = this.baseViewBox.height / this.baseViewBox.width;
      const nextHeight = nextWidth * aspect;
      const anchorX = this.viewBox.x + this.viewBox.width * sx;
      const anchorY = this.viewBox.y + this.viewBox.height * sy;
      this.viewBox = {
        x: anchorX - nextWidth * sx,
        y: anchorY - nextHeight * sy,
        width: nextWidth,
        height: nextHeight,
      };
      this.updateViewBox();
    }

    handleWheel(event) {
      event.preventDefault();
      this.zoomAt(event.deltaY > 0 ? 1.14 : .86, event.clientX, event.clientY);
    }

    handlePointerDown(event) {
      if (!this.viewBox) return;
      this.pointerState = {
        id: event.pointerId,
        clientX: event.clientX,
        clientY: event.clientY,
        viewBox: { ...this.viewBox },
      };
      this.dragDistance = 0;
      this.map.setPointerCapture?.(event.pointerId);
      this.map.classList.add("is-panning");
    }

    handlePointerMove(event) {
      if (!this.pointerState || this.pointerState.id !== event.pointerId) return;
      const rect = this.map.getBoundingClientRect();
      const dx = event.clientX - this.pointerState.clientX;
      const dy = event.clientY - this.pointerState.clientY;
      this.dragDistance = Math.max(this.dragDistance, Math.hypot(dx, dy));
      this.viewBox = {
        ...this.pointerState.viewBox,
        x: this.pointerState.viewBox.x - dx * this.pointerState.viewBox.width / rect.width,
        y: this.pointerState.viewBox.y - dy * this.pointerState.viewBox.height / rect.height,
      };
      this.updateViewBox();
    }

    handlePointerUp(event) {
      if (!this.pointerState || this.pointerState.id !== event.pointerId) return;
      this.map.releasePointerCapture?.(event.pointerId);
      this.pointerState = null;
      this.map.classList.remove("is-panning");
      window.setTimeout(() => {
        this.dragDistance = 0;
      }, 0);
    }

    updateViewBox() {
      if (!this.viewBox) return;
      this.map.setAttribute(
        "viewBox",
        `${this.viewBox.x} ${this.viewBox.y} ${this.viewBox.width} ${this.viewBox.height}`,
      );
    }

    destroy() {
      this.stopPlayback();
      if (this.refreshTimer) window.clearInterval(this.refreshTimer);
      this.refreshTimer = null;
      this.root.replaceChildren();
      delete this.root.__flowMindZoneExplorer;
    }
  }

  const mount = (
    root = document.getElementById("zoneExplorerRoot") || document.getElementById("zoneExplorer"),
    options = {},
  ) => {
    if (!root) return null;
    if (root.__flowMindZoneExplorer) return root.__flowMindZoneExplorer;
    const instance = new ZoneExplorer(root, options);
    root.__flowMindZoneExplorer = instance;
    const embeddedPayload = options.payload || window.FLOWMIND_ZONE_EXPLORER_PAYLOAD;
    if (embeddedPayload) {
      instance.setPayload(embeddedPayload);
    } else if (root.dataset.autoload !== "false" && options.autoload !== false) {
      instance.load();
    }
    return instance;
  };

  let defaultInstance = null;
  window.FlowMindZoneExplorer = {
    layout: {
      polylineMetrics,
      samplePolyline,
      nearestLaneId,
      orientedBox,
      boxesOverlap,
      buildStaticVehicleLayout,
    },
    mount: (root, options) => {
      defaultInstance = mount(root, options) || defaultInstance;
      return defaultInstance;
    },
    setPayload: (payload) => {
      defaultInstance = defaultInstance || mount();
      defaultInstance?.setPayload(payload);
      return defaultInstance;
    },
    load: () => {
      defaultInstance = defaultInstance || mount();
      return defaultInstance?.load();
    },
    selectTls: (tlsId) => defaultInstance?.selectTls(tlsId),
    destroy: () => {
      defaultInstance?.destroy();
      defaultInstance = null;
    },
  };

  const autoMount = () => {
    const root = document.getElementById("zoneExplorerRoot") || document.getElementById("zoneExplorer");
    if (root) defaultInstance = mount(root);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoMount, { once: true });
  } else {
    autoMount();
  }
})();
