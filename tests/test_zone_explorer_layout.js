"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

global.window = {
  CSS: { escape: (value) => String(value) },
  clearInterval,
  clearTimeout,
  setInterval,
  setTimeout,
};
global.document = {
  readyState: "loading",
  addEventListener: () => {},
  getElementById: () => null,
};

require("../dashboard/assets/zone_explorer.js");

const {
  boxesOverlap,
  buildStaticVehicleLayout,
} = global.window.FlowMindZoneExplorer.layout;

const projection = {
  markerRadius: 0,
  project: (x, y) => ({ x, y }),
  vehicleBreadth: 2,
  vehicleLength: 6,
};

const lane = (
  laneId,
  shape,
  vehicleCount,
  {
    directions = ["incoming"],
    occupancy = 0,
    queue = vehicleCount,
  } = {},
) => ({
  lane_id: laneId,
  shape,
  directions,
  vehicle_count: vehicleCount,
  queue,
  occupancy,
});

const vehicle = (vehicleId, laneId, x = 0, y = 0) => ({
  vehicle_id: vehicleId,
  lane_id: laneId,
  x,
  y,
  speed: 0,
  is_priority: false,
});

test("five vehicles form one deterministic, evenly spaced static queue", () => {
  const lanes = [lane("lane-east", [[0, 0], [100, 0]], 5)];
  const vehicles = [
    vehicle("vehicle-5", "lane-east"),
    vehicle("vehicle-2", "lane-east"),
    vehicle("vehicle-4", "lane-east"),
    vehicle("vehicle-1", "lane-east"),
    vehicle("vehicle-3", "lane-east"),
  ];

  const first = buildStaticVehicleLayout(lanes, vehicles, projection);
  const second = buildStaticVehicleLayout(lanes, vehicles, projection);

  assert.deepEqual(second, first);
  assert.equal(first.placements.length, 5);
  assert.deepEqual(
    first.placements.map(({ vehicle_id: vehicleId }) => vehicleId),
    ["vehicle-1", "vehicle-2", "vehicle-3", "vehicle-4", "vehicle-5"],
  );
  assert.ok(first.placements.every(({ lane_id: laneId, y }) => (
    laneId === "lane-east" && y === 0
  )));

  const distances = first.placements.map(({ lane_distance: distance }) => distance);
  for (let index = 1; index < distances.length; index += 1) {
    assert.ok(distances[index - 1] > distances[index]);
    assert.ok(distances[index - 1] - distances[index] >= 7.5);
  }
});

test("collision boxes never overlap when two populated lanes cross", () => {
  const lanes = [
    lane("horizontal", [[-80, 0], [80, 0]], 14, { occupancy: 0.9 }),
    lane("vertical", [[0, -80], [0, 80]], 14, { occupancy: 0.8 }),
  ];

  const result = buildStaticVehicleLayout(lanes, [], projection);
  const laneIds = new Set(result.placements.map(({ lane_id: laneId }) => laneId));

  assert.ok(result.placements.length >= 24);
  assert.deepEqual(laneIds, new Set(["horizontal", "vertical"]));
  for (let left = 0; left < result.placements.length; left += 1) {
    for (let right = left + 1; right < result.placements.length; right += 1) {
      assert.equal(
        boxesOverlap(
          result.placements[left].collision_box,
          result.placements[right].collision_box,
        ),
        false,
        `${result.placements[left].vehicle_id} overlaps ${result.placements[right].vehicle_id}`,
      );
    }
  }
});

test("an internal-lane vehicle snaps to the nearest visual lane deterministically", () => {
  const lanes = [
    lane("visual-near", [[0, 0], [100, 0]], 0),
    lane("visual-far", [[0, 30], [100, 30]], 0),
  ];
  const vehicles = [
    vehicle("internal-vehicle", ":junction_0", 35, 2),
  ];

  const first = buildStaticVehicleLayout(lanes, vehicles, projection);
  const second = buildStaticVehicleLayout(lanes, vehicles, projection);

  assert.deepEqual(second, first);
  assert.equal(first.stats.reassignedCount, 1);
  assert.equal(first.stats.unassignedCount, 0);
  assert.equal(first.placements.length, 1);
  assert.equal(first.placements[0].lane_id, "visual-near");
  assert.equal(first.placements[0].original_lane_id, ":junction_0");
});
