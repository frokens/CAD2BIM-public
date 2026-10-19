import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import { STLLoader } from './vendor/three/STLLoader.js';

// Sessions without a usable WebGL context (embedded browsers, GPU-disabled profiles, exhausted
// contexts) still have to show the model. The same exported triangles are projected with the canvas
// 2D API and painted back-to-front; the geometry is untouched, only the drawing path differs.
export function createFlatViewer(container, geometry, name) {
  const canvasHost = container.querySelector('.resource-model-canvas');
  const status = container.querySelector('.resource-model-status');
  const reset = container.querySelector('[data-view-reset]');
  const fallback = container.querySelector('.resource-model-fallback');
  const position = geometry?.getAttribute?.('position');
  const sphere = geometry?.boundingSphere;
  if (!canvasHost || !position || position.count < 3) return null;
  if (!sphere || !Number.isFinite(sphere.radius) || sphere.radius <= 0) return null;

  const triangles = [];
  const pointA = new THREE.Vector3(), pointB = new THREE.Vector3(), pointC = new THREE.Vector3();
  for (let i = 0; i + 2 < position.count; i += 3) {
    pointA.fromBufferAttribute(position, i);
    pointB.fromBufferAttribute(position, i + 1);
    pointC.fromBufferAttribute(position, i + 2);
    triangles.push([pointA.clone(), pointB.clone(), pointC.clone()]);
  }
  if (!triangles.length) return null;

  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d');
  if (!context) return null;
  canvas.className = 'resource-model-flat';
  canvas.tabIndex = 0;
  canvas.setAttribute('aria-label', `${name}三维模型静态视图，拖动旋转，滚轮缩放，Home 复位`);
  canvasHost.replaceChildren(canvas);

  const center = sphere.center, radius = sphere.radius;
  const light = new THREE.Vector3(0.34, -0.62, 0.71).normalize();
  const base = { r: 0x7f, g: 0x99, b: 0xad };
  const home = { yaw: -0.72, pitch: 0.5, zoom: 1 };
  let yaw = home.yaw, pitch = home.pitch, zoom = home.zoom, frame = 0, disposed = false;
  const rotated = triangles.map(() => [[0, 0, 0], [0, 0, 0], [0, 0, 0]]);
  const order = triangles.map((_, index) => index);
  const edgeA = new THREE.Vector3(), edgeB = new THREE.Vector3(), normal = new THREE.Vector3();

  const draw = () => {
    frame = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(canvasHost.clientWidth * dpr));
    const height = Math.max(1, Math.round(canvasHost.clientHeight * dpr));
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    const cosYaw = Math.cos(yaw), sinYaw = Math.sin(yaw), cosPitch = Math.cos(pitch), sinPitch = Math.sin(pitch);
    const scale = Math.min(width, height) * 0.42 / radius * zoom;
    for (let index = 0; index < triangles.length; index += 1) {
      const target = rotated[index];
      for (let corner = 0; corner < 3; corner += 1) {
        const source = triangles[index][corner];
        const x = source.x - center.x, y = source.y - center.y, z = source.z - center.z;
        const spinX = x * cosYaw - y * sinYaw, spinY = x * sinYaw + y * cosYaw;
        target[corner][0] = spinX;
        target[corner][1] = spinY * cosPitch - z * sinPitch;
        target[corner][2] = spinY * sinPitch + z * cosPitch;
      }
    }
    order.sort((left, right) => (rotated[left][0][2] + rotated[left][1][2] + rotated[left][2][2]) - (rotated[right][0][2] + rotated[right][1][2] + rotated[right][2][2]));
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.fillStyle = '#edf3f7';
    context.fillRect(0, 0, width, height);
    context.setTransform(scale, 0, 0, -scale, width / 2, height / 2);
    context.lineJoin = 'round';
    for (const index of order) {
      const [a, b, c] = rotated[index];
      edgeA.set(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
      edgeB.set(c[0] - a[0], c[1] - a[1], c[2] - a[2]);
      normal.copy(edgeA).cross(edgeB);
      const shade = normal.lengthSq() > 0 ? 0.42 + 0.58 * Math.abs(normal.normalize().dot(light)) : 0.42;
      const fill = `rgb(${Math.round(base.r * shade)},${Math.round(base.g * shade)},${Math.round(base.b * shade)})`;
      context.fillStyle = fill;
      context.strokeStyle = fill;
      context.lineWidth = 0.4 / scale + 0.0004;
      context.beginPath();
      context.moveTo(a[0], a[1]);
      context.lineTo(b[0], b[1]);
      context.lineTo(c[0], c[1]);
      context.closePath();
      context.fill();
      context.stroke();
    }
    if (fallback) fallback.hidden = true;
    if (status) status.textContent = '静态预览 · 当前环境未启用 WebGL';
    if (reset) reset.hidden = false;
  };
  const schedule = () => { if (!frame && !disposed) frame = requestAnimationFrame(draw); };

  let dragging = null;
  const onPointerDown = (event) => { dragging = { x: event.clientX, y: event.clientY }; canvas.setPointerCapture?.(event.pointerId); };
  const onPointerMove = (event) => {
    if (!dragging) return;
    yaw += (event.clientX - dragging.x) * 0.011;
    pitch = THREE.MathUtils.clamp(pitch - (event.clientY - dragging.y) * 0.011, -1.45, 1.45);
    dragging = { x: event.clientX, y: event.clientY };
    schedule();
  };
  const onPointerUp = () => { dragging = null; };
  const onWheel = (event) => { event.preventDefault(); zoom = THREE.MathUtils.clamp(zoom * (1 - event.deltaY * 0.0012), 0.5, 4); schedule(); };
  const onReset = () => { yaw = home.yaw; pitch = home.pitch; zoom = home.zoom; schedule(); };
  const onKey = (event) => {
    if (event.key === 'ArrowLeft') yaw -= 0.12;
    else if (event.key === 'ArrowRight') yaw += 0.12;
    else if (event.key === '+' || event.key === '=') zoom = THREE.MathUtils.clamp(zoom * 1.15, 0.5, 4);
    else if (event.key === '-') zoom = THREE.MathUtils.clamp(zoom / 1.15, 0.5, 4);
    else if (event.key === 'Home') onReset();
    else return;
    event.preventDefault();
    schedule();
  };
  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  canvas.addEventListener('pointerup', onPointerUp);
  canvas.addEventListener('pointercancel', onPointerUp);
  canvas.addEventListener('wheel', onWheel, { passive: false });
  canvas.addEventListener('keydown', onKey);
  const observer = new ResizeObserver(schedule);
  observer.observe(canvasHost);
  if (reset) reset.addEventListener('click', onReset);
  draw();
  return () => {
    disposed = true;
    if (frame) cancelAnimationFrame(frame);
    observer.disconnect();
    canvas.removeEventListener('pointerdown', onPointerDown);
    canvas.removeEventListener('pointermove', onPointerMove);
    canvas.removeEventListener('pointerup', onPointerUp);
    canvas.removeEventListener('pointercancel', onPointerUp);
    canvas.removeEventListener('wheel', onWheel);
    canvas.removeEventListener('keydown', onKey);
    reset?.removeEventListener('click', onReset);
    canvas.remove();
  };
}

// Render the exported geometry unchanged; camera fitting is presentation only.
export function mountViewer(container, model, name) {
  const abort = new AbortController();
  let renderer, controls, geometry, material, observer, flatDispose = null, disposed = false;
  const canvasHost = container.querySelector('.resource-model-canvas');
  const status = container.querySelector('.resource-model-status');
  const reset = container.querySelector('[data-view-reset]');
  const fallback = container.querySelector('.resource-model-fallback');
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#edf3f7');
  const camera = new THREE.PerspectiveCamera(35, 1, 0.001, 1000);
  camera.up.set(0, 0, 1);
  let fit = () => {};
  const draw = () => { if (!disposed && renderer) renderer.render(scene, camera); };
  const keyboard = (event) => {
    if (!controls) return;
    const offsets = { ArrowLeft: -0.12, ArrowRight: 0.12 };
    if (event.key in offsets) {
      const offset = camera.position.clone().sub(controls.target);
      offset.applyAxisAngle(camera.up, offsets[event.key]);
      camera.position.copy(controls.target).add(offset);
    } else if (event.key === '+' || event.key === '=' || event.key === '-') {
      const factor = event.key === '-' ? 1.15 : 1 / 1.15;
      const offset = camera.position.clone().sub(controls.target);
      offset.setLength(THREE.MathUtils.clamp(offset.length() * factor, controls.minDistance, controls.maxDistance));
      camera.position.copy(controls.target).add(offset);
    } else if (event.key === 'Home') { fit(); }
    else return;
    event.preventDefault();
    controls.update(); draw();
  };
  const onReset = () => { fit(); draw(); };
  const release = () => {
    flatDispose?.(); flatDispose = null;
    observer?.disconnect(); controls?.dispose(); geometry?.dispose(); material?.dispose();
    reset?.removeEventListener('click', onReset);
    renderer?.domElement.removeEventListener('keydown', keyboard);
    renderer?.dispose(); renderer?.forceContextLoss(); renderer?.domElement.remove();
    renderer = null; observer = null; controls = null; geometry = null; material = null;
  };
  // WebGL path: a real 3D view with orbit controls.
  const startWebGL = (sphere) => {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.domElement.tabIndex = 0;
    renderer.domElement.setAttribute('aria-label', `${name}三维模型，拖动旋转，滚轮缩放；左右方向键旋转，加减键缩放，Home 复位`);
    renderer.domElement.addEventListener('keydown', keyboard);
    canvasHost.append(renderer.domElement);
    material = new THREE.MeshStandardMaterial({ color: '#7f99ad', roughness: 0.62, metalness: 0.12, side: THREE.DoubleSide });
    scene.add(new THREE.Mesh(geometry, material));
    scene.add(new THREE.HemisphereLight(0xffffff, 0x617487, 2.6));
    const key = new THREE.DirectionalLight(0xffffff, 3.2);
    key.position.set(2, -3, 5); scene.add(key);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.target.copy(sphere.center);
    controls.enablePan = false;
    controls.minDistance = sphere.radius * 1.15;
    controls.maxDistance = sphere.radius * 18;
    controls.addEventListener('change', draw);
    fit = () => {
      const limitingFov = Math.min(THREE.MathUtils.degToRad(camera.fov), 2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.aspect));
      const distance = sphere.radius / Math.sin(limitingFov / 2) * 1.12;
      camera.position.copy(sphere.center).add(new THREE.Vector3(1, -1.4, 0.9).normalize().multiplyScalar(distance));
      camera.near = sphere.radius / 100; camera.far = sphere.radius * 100;
      camera.updateProjectionMatrix(); controls.target.copy(sphere.center); controls.update();
    };
    let fitted = false;
    observer = new ResizeObserver(() => {
      const { width, height } = canvasHost.getBoundingClientRect();
      if (!width || !height || disposed) return;
      camera.aspect = width / height; camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
      if (!fitted) { fit(); fitted = true; }
      draw();
      fallback.hidden = true;
      status.textContent = '拖动旋转 · 滚轮或双指缩放';
    });
    observer.observe(canvasHost);
    reset.hidden = false; reset.addEventListener('click', onReset);
  };
  (async () => {
    try {
      if (model.format !== 'stl') throw new Error('Unsupported model format');
      const response = await fetch(model.url, { signal: abort.signal });
      if (!response.ok) throw new Error(`Model response ${response.status}`);
      const bytes = await response.arrayBuffer();
      if (disposed) return;
      geometry = new STLLoader().parse(bytes);
      geometry.computeBoundingSphere();
      const sphere = geometry.boundingSphere;
      if (!sphere || !Number.isFinite(sphere.radius) || sphere.radius <= 0) throw new Error('Empty model');
      try {
        startWebGL(sphere);
      } catch (webglError) {
        // No usable WebGL context: draw the same exported triangles on a 2D canvas instead of
        // giving up on the model. Only if that is impossible too do we fall back to the image.
        renderer?.dispose?.(); renderer?.domElement?.remove?.(); renderer = null; controls = null; material = null;
        flatDispose = createFlatViewer(container, geometry, name);
        if (!flatDispose) throw webglError;
      }
    } catch (error) {
      if (disposed || error.name === 'AbortError') return;
      release();
      status.textContent = fallback.querySelector('img') ? '三维视图暂不可用，显示模型图片' : '三维视图暂不可用';
      reset.hidden = true;
    }
  })();
  return () => { disposed = true; abort.abort(); release(); };
}
