const test = require('node:test');
const assert = require('node:assert/strict');
const {
  clampViewerZoom,
  clampViewerPan,
  zoomViewerAroundPoint,
  pinchViewerTransform,
} = require('../app.js');

const baseRect = {left: 100, top: 100, width: 200, height: 100};
const viewportRect = {left: 50, top: 50, width: 300, height: 200};

test('clampViewerZoom keeps photo zoom within the bounded viewer range', () => {
  assert.equal(clampViewerZoom(0.25), 1);
  assert.equal(clampViewerZoom(1), 1);
  assert.equal(clampViewerZoom(3.25), 3.25);
  assert.equal(clampViewerZoom(5), 5);
  assert.equal(clampViewerZoom(7), 5);
});

test('clampViewerPan keeps a zoomed image covering the media viewport', () => {
  assert.deepEqual(
    clampViewerPan({x: 999, y: 999}, {baseRect: baseRect, viewportRect: viewportRect, zoom: 2}),
    {x: 50, y: 0},
  );
  assert.deepEqual(
    clampViewerPan({x: -999, y: -999}, {baseRect: baseRect, viewportRect: viewportRect, zoom: 2}),
    {x: -50, y: 0},
  );
  assert.deepEqual(
    clampViewerPan({x: 24, y: -18}, {baseRect: baseRect, viewportRect: viewportRect, zoom: 1}),
    {x: 0, y: 0},
  );
});

test('zoomViewerAroundPoint preserves the tapped image point while respecting bounds', () => {
  const result = zoomViewerAroundPoint({
    baseRect,
    viewportRect,
    currentZoom: 1,
    currentPan: {x: 0, y: 0},
    nextZoom: 2,
    point: {x: 150, y: 150},
  });

  assert.equal(result.zoom, 2);
  assert.deepEqual(result.pan, {x: 50, y: 0});
  const center = {x: 200, y: 150};
  const transformedPoint = {
    x: center.x + result.zoom * (150 - center.x) + result.pan.x,
    y: center.y + result.zoom * (150 - center.y) + result.pan.y,
  };
  assert.deepEqual(transformedPoint, {x: 150, y: 150});
});

test('pinchViewerTransform follows midpoint movement and distance growth', () => {
  const result = pinchViewerTransform({
    baseRect,
    viewportRect,
    startZoom: 1,
    startPan: {x: 0, y: 0},
    startDistance: 80,
    currentDistance: 160,
    startMidpoint: {x: 200, y: 150},
    currentMidpoint: {x: 150, y: 150},
  });

  assert.equal(result.zoom, 2);
  assert.deepEqual(result.pan, {x: -50, y: 0});
  const center = {x: 200, y: 150};
  const transformedStartMidpoint = {
    x: center.x + result.zoom * (200 - center.x) + result.pan.x,
    y: center.y + result.zoom * (150 - center.y) + result.pan.y,
  };
  assert.deepEqual(transformedStartMidpoint, {x: 150, y: 150});
});
