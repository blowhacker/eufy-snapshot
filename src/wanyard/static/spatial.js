(function () {
  'use strict';

  const canvas = document.getElementById('cloudCanvas');
  const depthImage = document.getElementById('depthImage');
  const fallbackImage = document.getElementById('fallbackImage');
  const loading = document.getElementById('loading');
  const loadingDetail = document.getElementById('loadingDetail');
  const sceneSelect = document.getElementById('sceneSelect');
  const state = { scenes: [], scene: null, run: null, viewer: null, sources: [], feasibility: null, previousFocus: null, runPoll: null };

  function artifactUrl(name) {
    const path = ['/api/spatial', state.scene.id, state.run.id, name]
      .map((part, index) => index ? encodeURIComponent(part) : part).join('/');
    return path + '?v=' + encodeURIComponent(state.run.updated_at || state.run.created_at || '1');
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
    const download = document.getElementById('downloadCloud');
    download.hidden = !run.artifacts.point_cloud;
    download.href = run.artifacts.point_cloud ? artifactUrl('point_cloud') : '#';
    document.getElementById('removeSpatialView').hidden = false;
    depthImage.src = artifactUrl('depth_preview');
    fallbackImage.src = artifactUrl('pointcloud_preview');
  }

  async function openScene(scene) {
    if (state.runPoll) { clearTimeout(state.runPoll); state.runPoll = null; }
    state.scene = scene;
    state.run = scene.runs[0];
    if (!state.run) return showError('This scene has no reconstruction runs.');
    renderMetadata();
    if (!state.run.artifacts || !state.run.artifacts.point_cloud) {
      if (state.viewer) { state.viewer.destroy(); state.viewer = null; }
      canvas.hidden = true;
      depthImage.hidden = true;
      fallbackImage.hidden = true;
      loading.hidden = false;
      loading.querySelector('span').hidden = true;
      loading.querySelector('strong').textContent = state.run.status === 'failed'
        ? 'Reconstruction failed'
        : state.run.status === 'running'
          ? 'Building spatial view'
          : 'Reconstruction queued';
      loadingDetail.textContent = state.run.error || (state.run.status === 'running'
        ? 'Matching and triangulating the shared camera view'
        : 'Waiting for the reconstruction worker');
      document.querySelector('.sp-stage-bottom').hidden = true;
      if (state.run.status === 'queued' || state.run.status === 'running') {
        const sceneId = scene.id;
        state.runPoll = setTimeout(async () => {
          state.runPoll = null;
          try {
            await refreshScenes();
            const updated = state.scenes.find(item => item.id === sceneId);
            if (updated && state.scene && state.scene.id === sceneId) {
              sceneSelect.value = sceneId;
              await openScene(updated);
            }
          } catch (error) {
            console.error(error);
          }
        }, 2000);
      }
      return;
    }
    document.querySelector('.sp-stage-bottom').hidden = false;
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
    const faceMatch = headerText.match(/element face (\d+)/);
    const faceCount = faceMatch ? Number(faceMatch[1]) : 0;
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
      positions[index] = -(positions[index] - center[0]) / scale * 2;
      positions[index + 1] = -(positions[index + 1] - center[1]) / scale * 2;
      positions[index + 2] = (positions[index + 2] - center[2]) / scale * 2;
    }
    const faceValues = [];
    let faceOffset = count * stride;
    for (let index = 0; index < faceCount; index++) {
      if (faceOffset >= view.byteLength) throw new Error('PLY face data is incomplete');
      const vertices = view.getUint8(faceOffset);
      faceOffset += 1;
      if (faceOffset + vertices * 4 > view.byteLength) throw new Error('PLY face data is incomplete');
      if (vertices === 3) {
        faceValues.push(
          view.getUint32(faceOffset, true),
          view.getUint32(faceOffset + 4, true),
          view.getUint32(faceOffset + 8, true)
        );
      }
      faceOffset += vertices * 4;
    }
    return { count, positions, colors, indices: faceValues.length ? new Uint32Array(faceValues) : null };
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
      uniform bool uRenderPoints;
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
      uniform bool uRenderPoints;
      varying vec3 vColor;
      void main() {
        if (uRenderPoints) {
          vec2 d = gl_PointCoord - vec2(.5);
          if (dot(d, d) > .25) discard;
        }
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
    ['uYaw', 'uPitch', 'uDistance', 'uAspect', 'uPointSize', 'uRenderPoints'].forEach(name => uniforms[name] = gl.getUniformLocation(program, name));
    let indexBuffer = null;
    let indexType = null;
    let indexCount = 0;
    if (cloud.indices && cloud.indices.length) {
      indexBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
      if (cloud.count <= 65535) {
        const compact = new Uint16Array(cloud.indices);
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, compact, gl.STATIC_DRAW);
        indexType = gl.UNSIGNED_SHORT;
      } else if (gl.getExtension('OES_element_index_uint')) {
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, cloud.indices, gl.STATIC_DRAW);
        indexType = gl.UNSIGNED_INT;
      } else {
        gl.deleteBuffer(indexBuffer);
        indexBuffer = null;
      }
      if (indexBuffer) indexCount = cloud.indices.length;
    }
    const view = { yaw: -.35, pitch: .12, distance: 3.0, pointSize: 2.25, orbiting: false };
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
      if (indexBuffer) {
        gl.uniform1i(uniforms.uRenderPoints, 0);
        gl.drawElements(gl.TRIANGLES, indexCount, indexType, 0);
      } else {
        gl.uniform1i(uniforms.uRenderPoints, 1);
        gl.drawArrays(gl.POINTS, 0, cloud.count);
      }
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
    return { destroy() { cancelAnimationFrame(animation); gl.deleteBuffer(positionBuffer); gl.deleteBuffer(colorBuffer); if (indexBuffer) gl.deleteBuffer(indexBuffer); gl.deleteProgram(program); } };
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

  const createModal = document.getElementById('spatialCreateModal');
  const cameraChoices = document.getElementById('spatialCameraList');
  const selectHint = document.getElementById('spatialSelectHint');
  const checkButton = document.getElementById('spatialCheck');
  const createButton = document.getElementById('spatialCreate');
  const feasibilityBox = document.getElementById('spatialFeasibility');
  const sceneNameInput = document.getElementById('spatialSceneName');

  function sourceId(source) { return source.id || source.camera_id || source.slug || source.name; }
  function sourceName(source) { return source.name || source.label || source.title || niceLabel(sourceId(source)); }
  function enabledSource(source) { return source.enabled !== false && source.active !== false && source.disabled !== true; }
  function selectedCameraIds() {
    return Array.from(cameraChoices.querySelectorAll('input:checked')).map(input => input.value);
  }
  function setCreateBusy(button, busy, label) {
    button.disabled = busy;
    if (label) button.textContent = label;
  }
  async function responseError(response, fallback) {
    const payload = await response.json().catch(() => ({}));
    return new Error(payload.error || fallback + ' (' + response.status + ')');
  }
  function updateSelection() {
    const count = selectedCameraIds().length;
    const enough = count >= 2;
    checkButton.disabled = !enough;
    if (!enough) selectHint.textContent = 'Select at least two cameras to check overlap.';
    else selectHint.textContent = count + ' cameras selected. Check overlap to continue.';
    state.feasibility = null;
    createButton.disabled = true;
    createButton.textContent = 'Create spatial view';
    feasibilityBox.hidden = true;
  }
  function renderCameraChoices() {
    const enabled = state.sources.filter(enabledSource);
    cameraChoices.replaceChildren(...enabled.map(source => {
      const label = document.createElement('label');
      label.className = 'sp-camera-choice';
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = sourceId(source); input.addEventListener('change', updateSelection);
      const name = document.createElement('span'); name.textContent = sourceName(source);
      label.append(input, name);
      return label;
    }));
    if (!enabled.length) selectHint.textContent = 'No enabled cameras are available.';
    updateSelection();
  }
  function showFeasibility(result) {
    const mergeable = result.mergeable === true;
    const edges = result.edges || result.pair_edges || result.pairs || [];
    const components = result.components || result.connected_components || [];
    const unavailable = !mergeable && edges.length > 0 && edges.every(edge => edge.status === 'error');
    feasibilityBox.hidden = false;
    feasibilityBox.className = 'sp-feasibility ' + (mergeable ? 'mergeable' : 'not-mergeable');
    feasibilityBox.replaceChildren();
    const title = document.createElement('strong');
    title.textContent = mergeable
      ? 'These cameras can form one spatial view.'
      : unavailable
        ? 'Camera frames are not ready yet.'
        : 'These cameras do not yet form one connected view.';
    feasibilityBox.append(title);
    const detail = document.createElement('p');
    const firstReason = edges.flatMap(edge => Array.isArray(edge.reasons) ? edge.reasons : []).find(Boolean);
    detail.textContent = result.message || (mergeable
      ? 'Overlap found across the selected camera set.'
      : unavailable
        ? (firstReason || 'Recorded frames are still becoming available. Try the check again shortly.')
        : 'Choose cameras with overlapping coverage, then check again.');
    feasibilityBox.append(detail);
    const rows = [];
    edges.forEach(edge => {
      const left = edge.left_camera_id || edge.from || edge.camera_a || edge.source_a || edge[0];
      const right = edge.right_camera_id || edge.to || edge.camera_b || edge.source_b || edge[1];
      const score = edge.score != null ? edge.score : edge.metrics && edge.metrics.score;
      const status = edge.status ? ' · ' + niceLabel(edge.status) : '';
      if (left && right) rows.push(String(left) + ' ↔ ' + String(right) + status + (score != null ? ' · ' + Math.round(score) : ''));
    });
    components.forEach((component, index) => {
      if (Array.isArray(component)) rows.push('Group ' + (index + 1) + ': ' + component.join(', '));
    });
    if (rows.length) {
      const list = document.createElement('ul'); list.className = 'sp-feasibility-list';
      rows.slice(0, 8).forEach(row => { const item = document.createElement('li'); item.textContent = row; list.append(item); });
      feasibilityBox.append(list);
    }
    createButton.disabled = !mergeable;
    createButton.textContent = 'Create spatial view';
  }
  async function loadSources() {
    const response = await fetch('/api/sources');
    if (!response.ok) throw new Error('Camera list returned ' + response.status);
    const payload = await response.json();
    state.sources = Array.isArray(payload) ? payload : (payload.sources || payload.cameras || []);
    renderCameraChoices();
  }
  async function openCreateModal() {
    state.previousFocus = document.activeElement;
    createModal.hidden = false;
    document.body.classList.add('sp-modal-open');
    feasibilityBox.hidden = true;
    try {
      selectHint.textContent = 'Loading enabled cameras…';
      await loadSources();
      sceneNameInput.focus();
    } catch (error) {
      console.error(error);
      selectHint.textContent = 'Unable to load cameras: ' + error.message;
    }
  }
  function closeCreateModal() {
    createModal.hidden = true;
    document.body.classList.remove('sp-modal-open');
    if (state.previousFocus && state.previousFocus.focus) state.previousFocus.focus();
  }
  async function checkFeasibility() {
    const cameraIds = selectedCameraIds();
    if (cameraIds.length < 2) return updateSelection();
    setCreateBusy(checkButton, true, 'Checking…');
    selectHint.textContent = 'Looking for shared views…';
    try {
      const response = await fetch('/api/spatial/feasibility', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ camera_ids: cameraIds })
      });
      if (!response.ok) throw await responseError(response, 'Overlap check failed');
      const payload = await response.json();
      state.feasibility = payload.feasibility || payload;
      showFeasibility(state.feasibility);
      const pairs = state.feasibility.pairs || [];
      const unavailable = pairs.length > 0 && pairs.every(pair => pair.status === 'error');
      selectHint.textContent = state.feasibility.mergeable
        ? 'Ready to create a spatial view.'
        : unavailable
          ? 'Frames were unavailable. Check again shortly.'
          : 'Select a connected camera set and check again.';
    } catch (error) {
      console.error(error);
      selectHint.textContent = 'Could not check overlap: ' + error.message;
    } finally {
      checkButton.disabled = selectedCameraIds().length < 2;
      checkButton.textContent = 'Check overlap';
    }
  }
  async function refreshScenes() {
    const response = await fetch('/api/spatial/scenes');
    if (!response.ok) throw new Error('Scene index returned ' + response.status);
    state.scenes = (await response.json()).scenes || [];
    sceneSelect.disabled = !state.scenes.length;
    sceneSelect.replaceChildren(...state.scenes.map(scene => {
      const option = document.createElement('option'); option.value = scene.id; option.textContent = scene.name; return option;
    }));
  }
  function showEmptySpatial() {
    if (state.runPoll) { clearTimeout(state.runPoll); state.runPoll = null; }
    state.scene = null; state.run = null;
    if (state.viewer) { state.viewer.destroy(); state.viewer = null; }
    canvas.hidden = true; depthImage.hidden = true; fallbackImage.hidden = true;
    document.querySelector('.sp-stage-bottom').hidden = true;
    loading.hidden = false;
    loading.querySelector('span').hidden = true;
    loading.querySelector('strong').textContent = 'No spatial views';
    loadingDetail.textContent = 'Select cameras to discover a shared view';
    sceneSelect.replaceChildren(new Option('No spatial views', ''));
    sceneSelect.disabled = true;
    document.getElementById('sceneName').textContent = 'Start from cameras';
    document.getElementById('runDescription').textContent = 'Choose camera views and Wanyard will test how they connect.';
    document.getElementById('pointCount').textContent = '—';
    document.getElementById('cameraCount').textContent = '—';
    document.getElementById('scaleType').textContent = '—';
    document.getElementById('cameraList').replaceChildren();
    document.getElementById('downloadCloud').hidden = true;
    document.getElementById('removeSpatialView').hidden = true;
  }
  async function removeCurrentScene() {
    if (!state.scene) return;
    const scene = state.scene;
    if (!confirm(`Remove ${scene.name} from Spatial? Its reconstruction files will be retained for recovery.`)) return;
    const button = document.getElementById('removeSpatialView');
    button.disabled = true; button.textContent = 'Removing…';
    try {
      const response = await fetch('/api/spatial/scenes/' + encodeURIComponent(scene.id), { method: 'DELETE' });
      if (!response.ok) throw await responseError(response, 'Unable to remove spatial view');
      await refreshScenes();
      if (state.scenes.length) {
        sceneSelect.disabled = false;
        sceneSelect.value = state.scenes[0].id;
        await openScene(state.scenes[0]);
      } else {
        showEmptySpatial();
        await openCreateModal();
      }
    } catch (error) {
      console.error(error);
      alert(error.message);
    } finally {
      button.disabled = false; button.textContent = 'Remove spatial view';
    }
  }
  async function createScene() {
    if (!state.feasibility || !state.feasibility.mergeable) return;
    const cameraIds = selectedCameraIds();
    const name = sceneNameInput.value.trim() || 'Spatial view';
    setCreateBusy(createButton, true, 'Creating…');
    selectHint.textContent = 'Starting reconstruction…';
    try {
      const response = await fetch('/api/spatial/scenes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, camera_ids: cameraIds, feasibility_id: state.feasibility.feasibility_id || state.feasibility.id })
      });
      if (!response.ok) throw await responseError(response, 'Scene creation failed');
      const result = await response.json();
      selectHint.textContent = result.message || 'Spatial view creation started.';
      await refreshScenes();
      const returnedScene = result.scene || result.spatial_scene;
      const scene = state.scenes.find(item => item.id === (returnedScene && returnedScene.id) || item.id === result.scene_id || item.id === result.id)
        || returnedScene || state.scenes.find(item => item.name === name);
      if (scene) {
        closeCreateModal();
        sceneSelect.value = scene.id;
        await openScene(scene);
      }
    } catch (error) {
      console.error(error);
      selectHint.textContent = 'Could not create the view: ' + error.message;
      createButton.disabled = false;
      createButton.textContent = 'Create spatial view';
    }
  }
  document.getElementById('newSpatialView').addEventListener('click', openCreateModal);
  document.querySelectorAll('[data-close-spatial-modal]').forEach(button => button.addEventListener('click', closeCreateModal));
  checkButton.addEventListener('click', checkFeasibility);
  createButton.addEventListener('click', createScene);
  document.getElementById('removeSpatialView').addEventListener('click', removeCurrentScene);
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !createModal.hidden) closeCreateModal(); });

  async function init() {
    try {
      await refreshScenes();
      if (!state.scenes.length) {
        showEmptySpatial();
        return openCreateModal();
      }
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
