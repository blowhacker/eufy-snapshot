(function () {
  'use strict';

  const canvas = document.getElementById('cloudCanvas');
  const depthImage = document.getElementById('depthImage');
  const fallbackImage = document.getElementById('fallbackImage');
  const loading = document.getElementById('loading');
  const loadingDetail = document.getElementById('loadingDetail');
  const sceneSelect = document.getElementById('sceneSelect');
  const state = { scenes: [], scene: null, run: null, viewer: null };

  function artifactUrl(name) {
    return ['/api/spatial', state.scene.id, state.run.id, name]
      .map((part, index) => index ? encodeURIComponent(part) : part).join('/');
  }

  function niceLabel(value) {
    return String(value).split(/[-_]+/).map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
  }

  function showError(message) {
    loading.hidden = false;
    loading.querySelector('span').hidden = true;
    loading.querySelector('strong').textContent = 'Unable to open reconstruction';
    loadingDetail.textContent = message;
  }

  function renderMetadata() {
    const scene = state.scene;
    const run = state.run;
    document.getElementById('sceneName').textContent = scene.name;
    document.getElementById('pointCount').textContent = Number(run.stats.points || 0).toLocaleString();
    document.getElementById('cameraCount').textContent = scene.camera_ids.length.toLocaleString();
    document.getElementById('scaleType').textContent = run.metric ? 'metric' : 'relative';
    document.getElementById('runKind').textContent = niceLabel(run.kind || 'reconstruction');
    document.getElementById('runBadge').textContent = niceLabel(run.kind || 'reconstruction');
    document.getElementById('runDate').textContent = run.created_at
      ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(run.created_at))
      : 'Unknown';
    document.getElementById('runStatus').innerHTML = '<i></i> ' + niceLabel(run.status || 'ready');
    document.getElementById('cameraList').replaceChildren(...scene.camera_ids.map(cameraId => {
      const chip = document.createElement('span');
      chip.className = 'sp-camera';
      chip.innerHTML = '<i></i>';
      chip.append(document.createTextNode(niceLabel(cameraId)));
      return chip;
    }));
    const warning = run.warnings && run.warnings[0];
    if (warning) document.querySelector('#warning p').textContent = warning;
    document.getElementById('downloadCloud').href = artifactUrl('point_cloud');
    depthImage.src = artifactUrl('depth_preview');
    fallbackImage.src = artifactUrl('pointcloud_preview');
  }

  async function openScene(scene) {
    state.scene = scene;
    state.run = scene.runs[0];
    if (!state.run) return showError('This scene has no reconstruction runs.');
    renderMetadata();
    loading.hidden = false;
    loading.querySelector('span').hidden = false;
    loading.querySelector('strong').textContent = 'Loading geometry';
    loadingDetail.textContent = 'Fetching point cloud';
    try {
      const response = await fetch(artifactUrl('point_cloud'));
      if (!response.ok) throw new Error('Point cloud returned ' + response.status);
      const buffer = await response.arrayBuffer();
      loadingDetail.textContent = 'Preparing ' + (runPointCount(buffer) || 'the') + ' points';
      const cloud = parsePly(buffer);
      if (state.viewer) state.viewer.destroy();
      state.viewer = createViewer(canvas, cloud);
      loading.hidden = true;
    } catch (error) {
      console.error(error);
      fallbackImage.hidden = false;
      canvas.hidden = true;
      loading.hidden = true;
      document.getElementById('interactionHint').textContent = 'Static preview · WebGL geometry unavailable';
    }
  }

  function runPointCount(buffer) {
    const header = new TextDecoder().decode(new Uint8Array(buffer, 0, Math.min(buffer.byteLength, 2048)));
    const match = header.match(/element vertex (\d+)/);
    return match ? Number(match[1]).toLocaleString() : null;
  }

  function parsePly(buffer) {
    const bytes = new Uint8Array(buffer);
    const headerText = new TextDecoder().decode(bytes.subarray(0, Math.min(bytes.length, 8192)));
    const endMarker = 'end_header';
    let offset = headerText.indexOf(endMarker);
    if (offset < 0 || !headerText.includes('format binary_little_endian 1.0')) {
      throw new Error('Only binary little-endian PLY point clouds are supported');
    }
    const countMatch = headerText.match(/element vertex (\d+)/);
    if (!countMatch) throw new Error('PLY has no vertex count');
    const count = Number(countMatch[1]);
    offset += endMarker.length;
    while (bytes[offset] === 10 || bytes[offset] === 13) offset++;
    const stride = 15;
    if (offset + count * stride > buffer.byteLength) throw new Error('PLY data is incomplete');
    const view = new DataView(buffer, offset);
    const positions = new Float32Array(count * 3);
    const colors = new Uint8Array(count * 3);
    const samples = [[], [], []];
    const sampleEvery = Math.max(1, Math.floor(count / 5000));
    for (let index = 0; index < count; index++) {
      const source = index * stride;
      const target = index * 3;
      positions[target] = view.getFloat32(source, true);
      positions[target + 1] = view.getFloat32(source + 4, true);
      positions[target + 2] = view.getFloat32(source + 8, true);
      colors[target] = view.getUint8(source + 12);
      colors[target + 1] = view.getUint8(source + 13);
      colors[target + 2] = view.getUint8(source + 14);
      if (index % sampleEvery === 0) {
        samples[0].push(positions[target]);
        samples[1].push(positions[target + 1]);
        samples[2].push(positions[target + 2]);
      }
    }
    samples.forEach(axis => axis.sort((a, b) => a - b));
    const lowIndex = Math.floor(samples[0].length * .01);
    const highIndex = Math.floor(samples[0].length * .99);
    const low = samples.map(axis => axis[lowIndex]);
    const high = samples.map(axis => axis[Math.min(highIndex, axis.length - 1)]);
    const center = low.map((value, axis) => (value + high[axis]) / 2);
    const scale = Math.max(...low.map((value, axis) => high[axis] - value)) || 1;
    for (let index = 0; index < positions.length; index += 3) {
      positions[index] = (positions[index] - center[0]) / scale * 2;
      positions[index + 1] = -(positions[index + 1] - center[1]) / scale * 2;
      positions[index + 2] = -(positions[index + 2] - center[2]) / scale * 2;
    }
    return { count, positions, colors };
  }

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    return shader;
  }

  function createViewer(target, cloud) {
    target.hidden = false;
    fallbackImage.hidden = true;
    const gl = target.getContext('webgl', { antialias: true, alpha: true });
    if (!gl) throw new Error('WebGL is unavailable');
    const vertexSource = `
      attribute vec3 aPosition;
      attribute vec3 aColor;
      uniform float uYaw;
      uniform float uPitch;
      uniform float uDistance;
      uniform float uAspect;
      uniform float uPointSize;
      varying vec3 vColor;
      void main() {
        float cy = cos(uYaw), sy = sin(uYaw);
        float cp = cos(uPitch), sp = sin(uPitch);
        vec3 p = aPosition;
        p = vec3(cy * p.x + sy * p.z, p.y, -sy * p.x + cy * p.z);
        p = vec3(p.x, cp * p.y - sp * p.z, sp * p.y + cp * p.z);
        p.z -= uDistance;
        float f = 1.92;
        float near = .05, far = 50.0;
        gl_Position = vec4(p.x * f / uAspect, p.y * f, ((far + near) / (near - far)) * p.z + (2.0 * far * near) / (near - far), -p.z);
        gl_PointSize = clamp(uPointSize * 3.5 / max(-p.z, .2), 1.0, 7.0);
        vColor = aColor;
      }`;
    const fragmentSource = `
      precision mediump float;
      varying vec3 vColor;
      void main() {
        vec2 d = gl_PointCoord - vec2(.5);
        if (dot(d, d) > .25) discard;
        gl_FragColor = vec4(vColor, .92);
      }`;
    const program = gl.createProgram();
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, vertexSource));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, fragmentSource));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);

    const positionBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, cloud.positions, gl.STATIC_DRAW);
    const positionLocation = gl.getAttribLocation(program, 'aPosition');
    gl.enableVertexAttribArray(positionLocation);
    gl.vertexAttribPointer(positionLocation, 3, gl.FLOAT, false, 0, 0);
    const colorBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, cloud.colors, gl.STATIC_DRAW);
    const colorLocation = gl.getAttribLocation(program, 'aColor');
    gl.enableVertexAttribArray(colorLocation);
    gl.vertexAttribPointer(colorLocation, 3, gl.UNSIGNED_BYTE, true, 0, 0);

    const uniforms = {};
    ['uYaw', 'uPitch', 'uDistance', 'uAspect', 'uPointSize'].forEach(name => uniforms[name] = gl.getUniformLocation(program, name));
    const view = { yaw: -.35, pitch: .12, distance: 3.0, pointSize: 2.25, orbiting: true };
    let pointer = null;
    let animation;
    let previousTime = performance.now();

    function resize() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.round(target.clientWidth * ratio);
      const height = Math.round(target.clientHeight * ratio);
      if (target.width !== width || target.height !== height) {
        target.width = width; target.height = height;
        gl.viewport(0, 0, width, height);
      }
    }
    function draw(time) {
      resize();
      if (view.orbiting && !pointer) view.yaw += Math.min(time - previousTime, 40) * .00007;
      previousTime = time;
      gl.clearColor(.025, .035, .045, 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST);
      gl.uniform1f(uniforms.uYaw, view.yaw);
      gl.uniform1f(uniforms.uPitch, view.pitch);
      gl.uniform1f(uniforms.uDistance, view.distance);
      gl.uniform1f(uniforms.uAspect, target.width / Math.max(target.height, 1));
      gl.uniform1f(uniforms.uPointSize, view.pointSize);
      gl.drawArrays(gl.POINTS, 0, cloud.count);
      animation = requestAnimationFrame(draw);
    }
    function reset() { view.yaw = -.35; view.pitch = .12; view.distance = 3.0; }
    target.addEventListener('pointerdown', event => {
      pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
      target.setPointerCapture(event.pointerId);
      target.classList.add('dragging');
    });
    target.addEventListener('pointermove', event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      view.yaw += (event.clientX - pointer.x) * .006;
      view.pitch = Math.max(-1.45, Math.min(1.45, view.pitch + (event.clientY - pointer.y) * .006));
      pointer.x = event.clientX; pointer.y = event.clientY;
    });
    function release(event) {
      if (pointer && pointer.id === event.pointerId) pointer = null;
      target.classList.remove('dragging');
    }
    target.addEventListener('pointerup', release);
    target.addEventListener('pointercancel', release);
    target.addEventListener('wheel', event => {
      event.preventDefault();
      view.distance = Math.max(1.15, Math.min(9, view.distance * Math.exp(event.deltaY * .001)));
    }, { passive: false });
    target.addEventListener('dblclick', reset);
    document.getElementById('pointSize').oninput = event => { view.pointSize = Number(event.target.value); };
    document.getElementById('resetView').onclick = reset;
    document.getElementById('orbitToggle').onclick = event => {
      view.orbiting = !view.orbiting;
      event.currentTarget.textContent = view.orbiting ? 'Orbiting' : 'Orbit paused';
      event.currentTarget.setAttribute('aria-pressed', String(view.orbiting));
    };
    animation = requestAnimationFrame(draw);
    return { destroy() { cancelAnimationFrame(animation); gl.deleteBuffer(positionBuffer); gl.deleteBuffer(colorBuffer); gl.deleteProgram(program); } };
  }

  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    const cloud = button.dataset.view === 'cloud';
    document.querySelectorAll('[data-view]').forEach(item => {
      item.classList.toggle('active', item === button);
      item.setAttribute('aria-pressed', String(item === button));
    });
    canvas.hidden = !cloud;
    depthImage.hidden = cloud;
    fallbackImage.hidden = true;
    document.querySelector('.sp-stage-bottom').hidden = !cloud;
  }));

  async function init() {
    try {
      const response = await fetch('/api/spatial/scenes');
      if (!response.ok) throw new Error('Scene index returned ' + response.status);
      state.scenes = (await response.json()).scenes || [];
      if (!state.scenes.length) return showError('No reconstruction artifacts have been published yet.');
      sceneSelect.replaceChildren(...state.scenes.map(scene => {
        const option = document.createElement('option');
        option.value = scene.id; option.textContent = scene.name;
        return option;
      }));
      sceneSelect.onchange = () => openScene(state.scenes.find(scene => scene.id === sceneSelect.value));
      await openScene(state.scenes[0]);
    } catch (error) {
      console.error(error);
      showError(error.message);
    }
  }

  init();
}());
