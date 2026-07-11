/* Live-only SUMO intersection monitor. No mock generator is used. */
(() => {
  "use strict";

  const STALE_AFTER_MS = 15_000;
  const MODE_LABELS = {
    static_fixed: "Static Fixed",
    sumo_actuated: "SUMO Actuated",
    local: "Local Adaptive",
    flowmind: "FlowMind Area Balance",
    fixed: "Fixed (legacy)",
  };
  const SIGNAL_LABELS = {
    green: "зелений",
    yellow: "жовтий",
    red: "червоний",
  };
  const LOAD_LABELS = {
    free: "вільно",
    busy: "середнє",
    critical: "високе",
  };
  const LOAD_COLORS = {
    free: "#62d48b",
    busy: "#f2bf5e",
    critical: "#ff6f61",
  };

  const asNumber = (value, fallback = 0) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };
  const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  const validPosition = (item) => (
    Number.isFinite(Number(item?.x)) && Number.isFinite(Number(item?.y))
  );
  const normaliseSignal = (value) => {
    const state = String(value || "").toLowerCase();
    if (state.includes("y")) return "yellow";
    if (state.includes("g")) return "green";
    return "red";
  };
  const loadLevel = (vehicleCount, queue, occupancy) => {
    if (vehicleCount >= 10 || queue >= 10 || occupancy >= .75) return "critical";
    if (vehicleCount >= 5 || queue >= 5 || occupancy >= .4) return "busy";
    return "free";
  };
  const shortTls = (_tlsId, index) => `I-${String(index + 1).padStart(2, "0")}`;

  class LiveZoneSimulation {
    constructor(root) {
      this.root = root;
      this.map = root.querySelector("#zoneSimulationMap");
      this.sourceTag = root.querySelector("#zoneSimulationSource");
      this.sceneSignature = "";
      this.intersectionNodes = new Map();
      this.intersectionLabels = new Map();
      this.selectedTlsId = null;
      this.lastScene = null;
      this.setInactive("waiting");
    }

    setSnapshot(payload) {
      const snapshot = payload?.zone_simulation;
      const simulation = payload?.system?.simulation || {};
      const status = String(snapshot?.status || simulation?.status || "waiting").toLowerCase();
      const emittedAt = Date.parse(String(payload?.emitted_at || ""));
      const fresh = Number.isFinite(emittedAt) && Date.now() - emittedAt <= STALE_AFTER_MS;

      if (!payload?.available || snapshot?.source !== "sumo") return this.setInactive("waiting");
      if (status !== "running") return this.setInactive(status);
      if (!fresh) return this.setInactive("stale");
      if (!snapshot?.active) return this.setInactive("no-active-intersections");

      const scene = this.normaliseSnapshot(snapshot);
      if (!scene) return this.setInactive("geometry-unavailable");
      this.render(scene);
    }

    normaliseSnapshot(snapshot) {
      const intersections = (Array.isArray(snapshot.intersections) ? snapshot.intersections : [])
        .filter(validPosition)
        .map((intersection, index) => ({
          ...intersection,
          id: String(intersection.tls_id || `tls-${index}`),
          label: shortTls(intersection.tls_id, index),
          x: Number(intersection.x),
          y: Number(intersection.y),
          signal: normaliseSignal(intersection.signal || intersection.state),
          queue: Math.max(0, Math.round(asNumber(intersection.incoming_queue))),
          incomingVehicles: Math.max(0, Math.round(asNumber(intersection.incoming_vehicles))),
          occupancy: clamp(asNumber(intersection.outgoing_occupancy), 0, 1),
          activeNow: Boolean(intersection.active_now),
        }));
      if (!intersections.length) return null;

      const laneMetrics = new Map();
      (Array.isArray(snapshot.lanes) ? snapshot.lanes : []).forEach((lane) => {
        const laneId = String(lane?.lane_id || "");
        if (!laneId) return;
        laneMetrics.set(laneId, {
          vehicleCount: Math.max(0, Math.round(asNumber(lane.vehicle_count))),
          queue: Math.max(0, Math.round(asNumber(lane.queue))),
          occupancy: clamp(asNumber(lane.occupancy), 0, 1),
        });
      });

      const laneOwners = new Map();
      intersections.forEach((intersection) => {
        (Array.isArray(intersection.movements) ? intersection.movements : []).forEach((movement) => {
          [movement.incoming_lane, movement.outgoing_lane].forEach((laneId) => {
            const key = String(laneId || "");
            if (key && !laneOwners.has(key)) laneOwners.set(key, intersection.id);
          });
        });
      });

      const nearestIntersection = (vehicle) => intersections.reduce((nearest, intersection) => {
        const distance = Math.hypot(vehicle.x - intersection.x, vehicle.y - intersection.y);
        return !nearest || distance < nearest.distance ? { id: intersection.id, distance } : nearest;
      }, null)?.id || intersections[0].id;

      const vehicles = (Array.isArray(snapshot.vehicles) ? snapshot.vehicles : [])
        .filter(validPosition)
        .map((vehicle) => {
          const laneId = String(vehicle.lane_id || "");
          const position = { x: Number(vehicle.x), y: Number(vehicle.y) };
          const metrics = laneMetrics.get(laneId) || { vehicleCount: 0, queue: 0, occupancy: 0 };
          return {
            id: String(vehicle.vehicle_id || ""),
            laneId,
            x: position.x,
            y: position.y,
            speed: Math.max(0, asNumber(vehicle.speed)),
            priority: Boolean(vehicle.is_priority),
            intersectionId: laneOwners.get(laneId) || nearestIntersection(position),
            laneMetrics: metrics,
          };
        });

      const vehiclesByIntersection = new Map(intersections.map((item) => [item.id, []]));
      vehicles.forEach((vehicle) => vehiclesByIntersection.get(vehicle.intersectionId)?.push(vehicle));
      intersections.forEach((intersection) => {
        const assigned = vehiclesByIntersection.get(intersection.id) || [];
        intersection.vehicleCount = Math.max(intersection.incomingVehicles, assigned.length);
        intersection.load = loadLevel(
          intersection.vehicleCount,
          intersection.queue,
          intersection.occupancy,
        );
        assigned.forEach((vehicle) => {
          const laneLoad = loadLevel(
            vehicle.laneMetrics.vehicleCount,
            vehicle.laneMetrics.queue,
            vehicle.laneMetrics.occupancy,
          );
          const rank = { free: 0, busy: 1, critical: 2 };
          vehicle.load = rank[laneLoad] > rank[intersection.load] ? laneLoad : intersection.load;
        });
      });

      return {
        mode: String(snapshot.mode || "").toLowerCase(),
        time: asNumber(snapshot.simulated_time),
        intersections,
        vehicles,
        vehiclesByIntersection,
      };
    }

    sceneKey(scene) {
      return scene.intersections.map((intersection) => intersection.id).join("|");
    }

    render(scene) {
      const key = this.sceneKey(scene);
      if (key !== this.sceneSignature) this.buildScene(scene, key);
      this.lastScene = scene;
      this.updateIntersections(scene);
      this.updateVehicles(scene);
      this.updateSummary(scene);
      this.renderPhases(scene);
      this.applySelection();
      this.setTag("SUMO live", "good");
      this.root.dataset.state = "active";
    }

    buildScene(scene, key) {
      this.sceneSignature = key;
      this.map.replaceChildren();
      this.map.classList.remove("is-inactive", "is-dragging");
      this.intersectionNodes.clear();
      this.intersectionLabels.clear();

      const grid = document.createElement("div");
      grid.className = "zone-sim-junction-grid";
      scene.intersections.forEach((intersection) => {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "zone-sim-junction-card";
        card.dataset.tlsId = intersection.id;
        card.setAttribute("aria-pressed", "false");
        card.title = `${intersection.label} · ${intersection.id}`;

        const header = document.createElement("span");
        header.className = "zone-sim-junction-header";
        const label = document.createElement("strong");
        label.textContent = intersection.label;
        const load = document.createElement("span");
        load.className = "zone-sim-junction-load";
        header.append(label, load);

        const miniMap = document.createElement("span");
        miniMap.className = "zone-sim-mini-map";
        const carLayer = document.createElement("span");
        carLayer.className = "zone-sim-mini-cars";
        const centre = document.createElement("span");
        centre.className = "zone-sim-mini-centre";
        const light = this.makeTrafficLight();
        miniMap.append(carLayer, centre, light);

        const stats = document.createElement("span");
        stats.className = "zone-sim-junction-stats";
        const vehicles = document.createElement("span");
        const queue = document.createElement("span");
        const occupancy = document.createElement("span");
        stats.append(vehicles, queue, occupancy);

        const tracking = document.createElement("span");
        tracking.className = "zone-sim-tracking-label";
        tracking.textContent = "Вибрати для відстеження";
        card.append(header, miniMap, stats, tracking);
        card.addEventListener("click", () => this.selectIntersection(intersection.id));
        grid.appendChild(card);

        this.intersectionNodes.set(intersection.id, {
          card, load, light, carLayer, vehicles, queue, occupancy, tracking,
        });
        this.intersectionLabels.set(intersection.id, intersection.label);
      });
      this.map.appendChild(grid);

      if (!this.selectedTlsId || !this.intersectionNodes.has(this.selectedTlsId)) {
        this.selectedTlsId = scene.intersections[0].id;
      }
    }

    makeTrafficLight() {
      const light = document.createElement("span");
      light.className = "zone-sim-light zone-sim-light--tile";
      ["red", "yellow", "green"].forEach((color) => {
        const lamp = document.createElement("i");
        lamp.className = `zone-sim-lamp zone-sim-lamp--${color}`;
        light.appendChild(lamp);
      });
      return light;
    }

    updateIntersections(scene) {
      scene.intersections.forEach((intersection) => {
        const nodes = this.intersectionNodes.get(intersection.id);
        if (!nodes) return;
        nodes.card.dataset.signal = intersection.signal;
        nodes.card.dataset.load = intersection.load;
        nodes.card.classList.toggle("is-live", intersection.activeNow);
        nodes.light.dataset.state = intersection.signal;
        nodes.load.textContent = LOAD_LABELS[intersection.load];
        nodes.vehicles.textContent = `${intersection.vehicleCount} авто`;
        nodes.queue.textContent = `черга ${intersection.queue}`;
        nodes.occupancy.textContent = `вихід ${Math.round(intersection.occupancy * 100)}%`;
      });
    }

    updateVehicles(scene) {
      scene.intersections.forEach((intersection) => {
        const nodes = this.intersectionNodes.get(intersection.id);
        if (!nodes) return;
        nodes.carLayer.replaceChildren();
        const vehicles = [...(scene.vehiclesByIntersection.get(intersection.id) || [])]
          .sort((left, right) => Number(right.priority) - Number(left.priority) || left.id.localeCompare(right.id));
        vehicles.slice(0, 12).forEach((vehicle, index) => {
          const car = document.createElement("i");
          car.className = "zone-sim-car zone-sim-mini-car";
          car.dataset.approach = String(index % 4);
          car.style.setProperty("--car-shift", `${Math.floor(index / 4) * 8}px`);
          car.style.setProperty("--car-color", vehicle.priority ? "#66b7ff" : LOAD_COLORS[vehicle.load]);
          car.title = `${vehicle.id} · ${vehicle.speed.toFixed(1)} м/с`;
          car.classList.toggle("is-waiting", vehicle.speed < .35);
          car.classList.toggle("is-priority", vehicle.priority);
          nodes.carLayer.appendChild(car);
        });
        if (vehicles.length > 12) {
          const overflow = document.createElement("span");
          overflow.className = "zone-sim-car-overflow";
          overflow.textContent = `+${vehicles.length - 12}`;
          nodes.carLayer.appendChild(overflow);
        }
      });
    }

    selectIntersection(tlsId) {
      if (!this.intersectionNodes.has(tlsId)) return;
      this.selectedTlsId = tlsId;
      this.applySelection();
      if (this.lastScene) this.updateSummary(this.lastScene);
    }

    applySelection() {
      this.intersectionNodes.forEach((nodes, tlsId) => {
        const selected = tlsId === this.selectedTlsId;
        nodes.card.classList.toggle("is-selected", selected);
        nodes.card.setAttribute("aria-pressed", String(selected));
        nodes.tracking.textContent = selected ? "● Відстежується live" : "Вибрати для відстеження";
      });
      this.root.querySelectorAll(".zone-sim-phase").forEach((row) => {
        row.classList.toggle("is-selected", row.dataset.tlsId === this.selectedTlsId);
      });
    }

    updateSummary(scene) {
      const selected = scene.intersections.find((item) => item.id === this.selectedTlsId)
        || scene.intersections[0];
      const queueTotal = scene.intersections.reduce((sum, item) => sum + item.queue, 0);
      this.setText("zoneSimulationPressure", String(scene.intersections.length));
      this.setText("zoneSimulationFreeSpace", String(scene.vehicles.length));
      this.setText("zoneSimulationQueue", `${queueTotal} авто`);
      this.setText(
        "zoneSimulationDirection",
        `${selected.label} · ${SIGNAL_LABELS[selected.signal]} · ${LOAD_LABELS[selected.load]}`,
      );
      this.setText("zoneSimulationDirectionIcon", "◎");
      this.setText("zoneSimulationMode", `Режим SUMO: ${MODE_LABELS[scene.mode] || scene.mode || "—"}`);
      this.setText(
        "zoneSimulationMessage",
        `Відстежується ${selected.label}: ${selected.vehicleCount} авто, черга ${selected.queue}, зайнятість виходу ${Math.round(selected.occupancy * 100)}% · SUMO ${Math.round(scene.time)} с.`,
      );
    }

    renderPhases(scene) {
      const container = this.root.querySelector("#zoneSimulationPhases");
      if (!container) return;
      container.replaceChildren();
      scene.intersections.forEach((intersection) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "zone-sim-phase";
        row.dataset.state = intersection.signal;
        row.dataset.tlsId = intersection.id;
        row.addEventListener("click", () => this.selectIntersection(intersection.id));
        const dot = document.createElement("i");
        const title = document.createElement("strong");
        title.textContent = intersection.label;
        const detail = document.createElement("span");
        detail.textContent = `${SIGNAL_LABELS[intersection.signal]} · ${intersection.vehicleCount} авто · q ${intersection.queue}`;
        row.append(dot, title, detail);
        container.appendChild(row);
      });
    }

    setInactive(reason) {
      const messages = {
        waiting: "Очікуємо запуск SUMO та перший live snapshot.",
        starting: "SUMO запускається. Монітор активується після появи контрольованої зони.",
        "no-active-intersections": "SUMO працює, але контрольована зона зараз неактивна.",
        "geometry-unavailable": "SUMO snapshot не містить перехресть контрольованої зони.",
        stale: "Останній SUMO snapshot застарів. Live-монітор вимкнено.",
        completed: "SUMO-симуляція завершена. Live-монітор вимкнено.",
        failed: "SUMO-симуляція завершилась з помилкою. Live-монітор вимкнено.",
        archive: "Для архівного запуску live-монітор вимкнений.",
      };
      const message = messages[reason] || "Live SUMO-монітор зараз недоступний.";
      this.sceneSignature = "";
      this.lastScene = null;
      this.intersectionNodes.clear();
      this.intersectionLabels.clear();
      if (this.map) {
        this.map.replaceChildren();
        this.map.classList.add("is-inactive");
        const placeholder = document.createElement("div");
        placeholder.className = "zone-sim-empty";
        placeholder.innerHTML = `<strong>Live SUMO монітор неактивний</strong><span>${message}</span>`;
        this.map.appendChild(placeholder);
      }
      this.root.dataset.state = "inactive";
      this.setTag(reason === "waiting" || reason === "starting" ? "очікує SUMO" : "SUMO inactive", "warn");
      ["zoneSimulationPressure", "zoneSimulationFreeSpace", "zoneSimulationQueue", "zoneSimulationDirection"]
        .forEach((id) => this.setText(id, "—"));
      this.setText("zoneSimulationDirectionIcon", "◎");
      this.setText("zoneSimulationMode", "Режим: очікує live SUMO");
      this.setText("zoneSimulationMessage", message);
      const phases = this.root.querySelector("#zoneSimulationPhases");
      if (phases) phases.innerHTML = '<div class="zone-sim-phase zone-sim-phase--empty">Немає live-даних світлофорів.</div>';
    }

    setTag(text, kind) {
      if (!this.sourceTag) return;
      this.sourceTag.className = `tag ${kind}`;
      this.sourceTag.textContent = text;
    }

    setText(id, value) {
      const node = this.root.querySelector(`#${id}`);
      if (node) node.textContent = value;
    }

    destroy() {
      this.map?.replaceChildren();
      this.intersectionNodes.clear();
      this.lastScene = null;
    }
  }

  let instance = null;
  const mount = (root = document.getElementById("zoneSimulation")) => {
    if (!root) return null;
    if (!instance) instance = new LiveZoneSimulation(root);
    return instance;
  };
  window.FlowMindZoneSimulation = {
    mount,
    setSnapshot: (payload) => mount()?.setSnapshot(payload),
    destroy: () => {
      instance?.destroy();
      instance = null;
    },
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mount(), { once: true });
  } else {
    mount();
  }
})();
