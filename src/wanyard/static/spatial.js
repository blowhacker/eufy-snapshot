(function () {
  'use strict';

  const canvas = document.getElementById('cloudCanvas');
  const depthImage = document.getElementById('depthImage');
  const fallbackImage = document.getElementById('fallbackImage');
  const loading = document.getElementById('loading');
  const loadingDetail = document.getElementById('loadingDetail');
  const sceneSelect = document.getElementById('sceneSelect');
  const refreshButton = document.getElementById('refreshGeometry');
  const densitySelect = document.getElementById('geometryDensity');
  const refreshMessage = document.getElementById('refreshMessage');
  const state = { scenes: [], scene: null, run: null, pendingRun: null, viewer: null, sources: [], feasibility: null, previousFocus: null, runPoll: null };

  function artifactUrl(name, scene = state.scene, run = state.run) {
    const path = ['/api/spatial', scene.id, run.id, name]
      .map((part, index) => index ? encodeURIComponent(part) : part).join('/');
    return path + '?v=' + encodeURIComponent(run.updated_at || run.created_at || '1');
  }

  function niceLabel(value) {
    return String(value).split(/[-_]+/).map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
  }

  function densityForRun(run) {
    if (['standard', 'high', 'full'].includes(run?.density_preset)) {
      return run.density_preset;
    }
    const budget = Number(run?.point_budget || run?.stats?.point_budget || 120000);
    if (budget <= 120000) return 'standard';
    if (budget >= 2000000) return 'full';
    return 'high';
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
    const runs = sceneRunState(scene);
    const selectedDensity = densityForRun(runs.pending || runs.failure || run);
    densitySelect.value = selectedDensity;
    document.getElementById('cameraCount').textContent = scene.camera_ids.length.toLocaleString();
    document.getElementById('scaleType').textContent = run.metric ? 'metric' : 'relative';
    document.getElementById('runKind').textContent = niceLabel(run.kind || 'reconstruction');
    document.getElementById('runBadge').textContent = niceLabel(run.kind || 'reconstruction');
    document.getElementById('runDate').textContent = run.created_at
      ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(run.created_at))
      : 'Unknown';
    const runPercentile = run.confidence_percentile ?? run.stats.confidence_percentile;
    const densityDetail = runPercentile == null
      ? `Legacy · ${Number(run.point_budget || run.stats.point_budget || 120000).toLocaleString()} cap`
      : Number(runPercentile) === 0
        ? 'Full · all finite'
        : `${niceLabel(densityForRun(run))} · confidence p${Number(runPercentile)}`;
    document.getElementById('runDensity').textContent = densityDetail;
    const candidatePoints = Number(run.stats.candidate_points || 0);
    document.getElementById('pointCount').title = candidatePoints
      ? `${Number(run.stats.points || 0).toLocaleString()} rendered from ${candidatePoints.toLocaleString()} valid model points`
      : '';
    document.getElementById('runStatus').innerHTML = '<i></i> ' + niceLabel(run.status || 'ready');
    document.getElementById('cameraList').replaceChildren(...scene.camera_ids.map(cameraId => {
      const chip = document.createElement('span');
      chip.className = 'sp-camera';
      chip.innerHTML = '<i></i>';
      chip.append(document.createTextNode(niceLabel(cameraId)));
      return chip;
    }));
    const warnings = (run.warnings || []).filter(message =>
      !String(message).includes('measurements wait for camera calibration')
    );
    const warningBox = document.getElementById('warning');
    warningBox.hidden = !warnings.length;
    if (warnings.length) warningBox.querySelector('p').textContent = warnings[0];
    const download = document.getElementById('downloadCloud');
    download.hidden = !run.artifacts.point_cloud;
    download.href = run.artifacts.point_cloud ? artifactUrl('point_cloud') : '#';
    document.getElementById('removeSpatialView').hidden = false;
    depthImage.src = artifactUrl('depth_preview');
    fallbackImage.src = artifactUrl('pointcloud_preview');
  }

  function sceneRunState(scene) {
    const runs = scene.runs || [];
    const pending = runs.find(run => run.status === 'queued' || run.status === 'running') || null;
    const ready = runs.find(run => run.status === 'ready' && run.artifacts?.point_cloud) || null;
    const latest = runs[0] || null;
    const failure = !pending && latest?.status === 'failed'
      && (!ready || String(latest.created_at || '') > String(ready.created_at || ''))
      ? latest : null;
    return { pending, ready, failure, display: ready || pending || latest };
  }

  function elapsedSeconds(run) {
    const started = Date.parse(run.updated_at || run.created_at || '');
    return Number.isFinite(started) ? Math.max(0, Math.round((Date.now() - started) / 1000)) : 0;
  }

  function renderRefreshState(pending, failure = null) {
    state.pendingRun = pending;
    const runBadge = document.getElementById('runBadge');
    runBadge.classList.toggle('busy', Boolean(pending));
    if (pending) {
      const running = pending.status === 'running';
      const status = running ? `Reconstructing · ${elapsedSeconds(pending)}s` : 'Geometry queued';
      refreshButton.disabled = true;
      densitySelect.disabled = true;
      refreshButton.textContent = status;
      refreshMessage.hidden = false;
      refreshMessage.className = 'sp-refresh-message busy';
      refreshMessage.textContent = running
        ? 'Building a replacement. The current live model remains active.'
        : 'Waiting for the reconstruction worker. The current model remains active.';
      runBadge.textContent = status;
      return;
    }
    refreshButton.disabled = !(state.scene && state.run?.artifacts?.point_cloud);
    densitySelect.disabled = refreshButton.disabled;
    refreshButton.textContent = failure ? 'Retry geometry' : 'Refresh geometry';
    if (failure) {
      refreshMessage.hidden = false;
      refreshMessage.className = 'sp-refresh-message failed';
      refreshMessage.textContent = `Refresh failed: ${failure.error || 'reconstruction failed'}. Existing geometry retained.`;
    } else {
      refreshMessage.hidden = true;
      refreshMessage.textContent = '';
    }
    if (state.run) runBadge.textContent = niceLabel(state.run.kind || 'reconstruction');
  }

  function scheduleRunPoll(sceneId) {
    if (state.runPoll) clearTimeout(state.runPoll);
    state.runPoll = setTimeout(async () => {
      state.runPoll = null;
      try {
        await refreshScenes();
        const updated = state.scenes.find(item => item.id === sceneId);
        if (updated && state.scene?.id === sceneId) {
          sceneSelect.value = sceneId;
          await openScene(updated);
        }
      } catch (error) {
        console.error(error);
        if (state.scene?.id === sceneId) scheduleRunPoll(sceneId);
      }
    }, 1500);
  }

  async function openScene(scene) {
    if (state.runPoll) { clearTimeout(state.runPoll); state.runPoll = null; }
    const previousSceneId = state.scene?.id;
    const previousRun = state.run;
    const previousViewer = state.viewer;
    state.scene = scene;
    const runs = sceneRunState(scene);
    if (!runs.display) return showError('This scene has no reconstruction runs.');

    if (!runs.ready) {
      state.run = runs.display;
      renderMetadata();
      renderRefreshState(runs.pending, runs.failure);
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
      if (runs.pending) scheduleRunPoll(scene.id);
      return;
    }

    const alreadyLoaded = previousSceneId === scene.id
      && previousRun?.id === runs.ready.id && Boolean(previousViewer);
    if (alreadyLoaded) {
      state.run = runs.ready;
      renderMetadata();
      renderRefreshState(runs.pending, runs.failure);
      document.querySelector('.sp-stage-bottom').hidden = false;
      if (runs.pending) scheduleRunPoll(scene.id);
      return;
    }

    const replacingCurrent = previousSceneId === scene.id && Boolean(previousViewer);
    if (!replacingCurrent && state.viewer) {
      state.viewer.destroy();
      state.viewer = null;
    }
    document.querySelector('.sp-stage-bottom').hidden = false;
    if (!replacingCurrent) {
      loading.hidden = false;
      loading.querySelector('span').hidden = false;
      loading.querySelector('strong').textContent = 'Loading geometry';
      loadingDetail.textContent = 'Fetching point cloud';
    } else {
      refreshMessage.hidden = false;
      refreshMessage.className = 'sp-refresh-message busy';
      refreshMessage.textContent = 'New geometry ready · validating before swap.';
    }
    try {
      const [response, liveMapResponse, modelSummary] = await Promise.all([
        fetch(artifactUrl('point_cloud', scene, runs.ready)),
        runs.ready.artifacts.live_map
          ? fetch(artifactUrl('live_map', scene, runs.ready)) : Promise.resolve(null),
        runs.ready.artifacts.model_summary
          ? fetch(artifactUrl('model_summary', scene, runs.ready))
            .then(result => result.ok ? result.json() : null)
            .catch(() => null)
          : Promise.resolve(null),
      ]);
      if (!response.ok) throw new Error('Point cloud returned ' + response.status);
      const buffer = await response.arrayBuffer();
      if (!replacingCurrent) loadingDetail.textContent = 'Preparing ' + (runPointCount(buffer) || 'the') + ' points';
      const cloud = parsePly(buffer);
      const liveMap = liveMapResponse && liveMapResponse.ok
        ? parseLiveMap(await liveMapResponse.arrayBuffer(), cloud.count)
        : null;
      if (state.scene?.id !== scene.id) return;
      const liveCameraIds = runs.ready.stats?.reconstructed_camera_ids || scene.camera_ids;
      const previousView = replacingCurrent ? state.viewer?.snapshot() : null;
      if (state.viewer) state.viewer.destroy();
      state.run = runs.ready;
      state.viewer = createViewer(canvas, cloud, liveMap, liveCameraIds, previousView, modelSummary);
      renderMetadata();
      renderRefreshState(runs.pending, runs.failure);
      setArtifactView('cloud');
      loading.hidden = true;
    } catch (error) {
      console.error(error);
      if (replacingCurrent) {
        state.run = previousRun;
        renderMetadata();
        renderRefreshState(null, { error: `new geometry could not be loaded (${error.message})` });
      } else {
        state.run = runs.ready;
        renderMetadata();
        fallbackImage.hidden = false;
        canvas.hidden = true;
        loading.hidden = true;
        document.getElementById('interactionHint').textContent = 'Static preview · WebGL geometry unavailable';
      }
    }
    if (runs.pending && state.scene?.id === scene.id) scheduleRunPoll(scene.id);
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

  function parseLiveMap(buffer, expectedPoints) {
    if (buffer.byteLength < 12) throw new Error('Live texture map is incomplete');
    const bytes = new Uint8Array(buffer);
    if (String.fromCharCode(...bytes.subarray(0, 4)) !== 'WYLM') {
      throw new Error('Live texture map has an unknown format');
    }
    const view = new DataView(buffer);
    const version = view.getUint16(4, true);
    const cameraCount = view.getUint16(6, true);
    const pointCount = view.getUint32(8, true);
    const stride = 12;
    if (version !== 1 || pointCount !== expectedPoints || 12 + pointCount * stride > buffer.byteLength) {
      throw new Error('Live texture map does not match this point cloud');
    }
    const uv = new Float32Array(pointCount * 2);
    const cameras = new Float32Array(pointCount);
    for (let index = 0; index < pointCount; index++) {
      const source = 12 + index * stride;
      uv[index * 2] = view.getFloat32(source, true);
      uv[index * 2 + 1] = view.getFloat32(source + 4, true);
      cameras[index] = view.getUint8(source + 8);
    }
    return { uv, cameras, cameraCount };
  }

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    return shader;
  }

  let hlsLoader;
  function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsLoader) hlsLoader = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/hls.min.js';
      script.onload = () => resolve(window.Hls);
      script.onerror = reject;
      document.head.appendChild(script);
    });
    return hlsLoader;
  }

  function makeLiveVideo() {
    const video = document.createElement('video');
    video.muted = true; video.autoplay = true; video.playsInline = true;
    video.setAttribute('muted', ''); video.setAttribute('playsinline', '');
    video.hidden = true;
    document.body.appendChild(video);
    return video;
  }

  async function attachSpatialLiveVideo(video, cameraId, resources) {
    try {
      const pc = new RTCPeerConnection();
      resources.peerConnections.push(pc);
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.ontrack = event => {
        video.srcObject = event.streams[0];
        video.play().catch(() => {});
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const response = await fetch(`/video/webrtc/${encodeURIComponent(cameraId)}/whep`, {
        method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: pc.localDescription.sdp,
      });
      if (!response.ok) throw new Error('WebRTC returned ' + response.status);
      await pc.setRemoteDescription({ type: 'answer', sdp: await response.text() });
      return;
    } catch (error) {
      console.warn('Spatial WebRTC fallback for ' + cameraId, error);
    }
    const url = `/video/native-live/${encodeURIComponent(cameraId)}/index.m3u8`;
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url; video.load(); video.play().catch(() => {}); return;
    }
    const Hls = await loadHls().catch(() => null);
    if (Hls?.isSupported?.()) {
      const hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 2 });
      resources.hls.push(hls);
      hls.loadSource(url); hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
    }
  }

  function createViewer(target, cloud, liveMap, cameraIds, initialView = null, modelSummary = null) {
    target.hidden = false;
    fallbackImage.hidden = true;
    const gl = target.getContext('webgl', { antialias: true, alpha: true });
    if (!gl) throw new Error('WebGL is unavailable');
    const vertexSource = `
      attribute vec3 aPosition;
      attribute vec3 aColor;
      attribute vec2 aLiveUv;
      attribute float aCamera;
      uniform float uYaw;
      uniform float uPitch;
      uniform float uDistance;
      uniform float uAspect;
      uniform float uPointSize;
      uniform bool uRenderPoints;
      varying vec3 vColor;
      varying vec2 vLiveUv;
      varying float vCamera;
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
        vLiveUv = aLiveUv;
        vCamera = aCamera;
      }`;
    const fragmentSource = `
      precision mediump float;
      uniform bool uRenderPoints;
      uniform bool uUseLive;
      uniform float uAtlasGrid;
      uniform sampler2D uLiveAtlas;
      varying vec3 vColor;
      varying vec2 vLiveUv;
      varying float vCamera;
      void main() {
        if (uRenderPoints) {
          vec2 d = gl_PointCoord - vec2(.5);
          if (dot(d, d) > .25) discard;
        }
        vec3 colour = vColor;
        if (uUseLive) {
          float column = mod(vCamera, uAtlasGrid);
          float row = floor(vCamera / uAtlasGrid);
          vec2 atlasUv = vec2(
            (column + vLiveUv.x) / uAtlasGrid,
            (uAtlasGrid - row - vLiveUv.y) / uAtlasGrid
          );
          colour = texture2D(uLiveAtlas, atlasUv).rgb;
        }
        gl_FragColor = vec4(colour, .92);
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

    const liveUvBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, liveUvBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, liveMap?.uv || new Float32Array(cloud.count * 2), gl.STATIC_DRAW);
    const liveUvLocation = gl.getAttribLocation(program, 'aLiveUv');
    gl.enableVertexAttribArray(liveUvLocation);
    gl.vertexAttribPointer(liveUvLocation, 2, gl.FLOAT, false, 0, 0);
    const cameraBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, cameraBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, liveMap?.cameras || new Float32Array(cloud.count), gl.STATIC_DRAW);
    const cameraLocation = gl.getAttribLocation(program, 'aCamera');
    gl.enableVertexAttribArray(cameraLocation);
    gl.vertexAttribPointer(cameraLocation, 1, gl.FLOAT, false, 0, 0);

    const uniforms = {};
    ['uYaw', 'uPitch', 'uDistance', 'uAspect', 'uPointSize', 'uRenderPoints', 'uUseLive', 'uAtlasGrid', 'uLiveAtlas'].forEach(name => uniforms[name] = gl.getUniformLocation(program, name));
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
    const liveResources = { peerConnections: [], hls: [], videos: [] };
    const liveAvailable = Boolean(
      liveMap && cameraIds?.length && liveMap.cameraCount === cameraIds.length
    );
    const atlasGrid = Math.max(1, Math.ceil(Math.sqrt(liveMap?.cameraCount || 1)));
    const atlasCell = 320;
    const atlas = document.createElement('canvas');
    atlas.width = atlas.height = atlasGrid * atlasCell;
    const atlasContext = atlas.getContext('2d', { alpha: false });
    const liveTexture = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, liveTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    atlasContext.fillStyle = '#111820'; atlasContext.fillRect(0, 0, atlas.width, atlas.height);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, atlas);
    if (liveAvailable) cameraIds.forEach(cameraId => {
      const video = makeLiveVideo();
      liveResources.videos.push(video);
      attachSpatialLiveVideo(video, cameraId, liveResources).catch(error => {
        console.warn('Spatial live video failed for ' + cameraId, error);
      });
    });
    let lastAtlasUpload = 0;
    let atlasReady = false;
    function updateLiveAtlas(time) {
      if (!view.live || !liveAvailable || time - lastAtlasUpload < 100) return;
      if (!liveResources.videos.every(video => video.readyState >= 2 && video.videoWidth)) return;
      lastAtlasUpload = time;
      liveResources.videos.forEach((video, index) => {
        const column = index % atlasGrid;
        const row = Math.floor(index / atlasGrid);
        atlasContext.drawImage(video, column * atlasCell, row * atlasCell, atlasCell, atlasCell);
      });
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, liveTexture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, atlas);
      atlasReady = true;
      liveToggle.dataset.ready = 'true';
      liveToggle.textContent = 'Live colour · on';
    }
    const defaultView = window.WanyardSpatialView?.cameraAlignedOrbit(modelSummary)
      || { yaw: Math.PI, pitch: 0 };
    const view = {
      yaw: initialView?.yaw ?? defaultView.yaw,
      pitch: initialView?.pitch ?? defaultView.pitch,
      distance: initialView?.distance ?? 3.0,
      pointSize: initialView?.pointSize ?? 2.25,
      orbiting: initialView?.orbiting ?? false,
      live: liveAvailable && initialView?.live !== false,
    };
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
      updateLiveAtlas(time);
      gl.uniform1i(uniforms.uUseLive, view.live && atlasReady ? 1 : 0);
      gl.uniform1f(uniforms.uAtlasGrid, atlasGrid);
      gl.uniform1i(uniforms.uLiveAtlas, 0);
      if (indexBuffer) {
        gl.uniform1i(uniforms.uRenderPoints, 0);
        gl.drawElements(gl.TRIANGLES, indexCount, indexType, 0);
      } else {
        gl.uniform1i(uniforms.uRenderPoints, 1);
        gl.drawArrays(gl.POINTS, 0, cloud.count);
      }
      animation = requestAnimationFrame(draw);
    }
    function reset() {
      view.yaw = defaultView.yaw;
      view.pitch = defaultView.pitch;
      view.distance = 3.0;
    }
    function pointerDown(event) {
      pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
      target.setPointerCapture(event.pointerId);
      target.classList.add('dragging');
    }
    function pointerMove(event) {
      if (!pointer || pointer.id !== event.pointerId) return;
      view.yaw += (event.clientX - pointer.x) * .006;
      view.pitch = Math.max(-1.45, Math.min(1.45, view.pitch + (event.clientY - pointer.y) * .006));
      pointer.x = event.clientX; pointer.y = event.clientY;
    }
    function release(event) {
      if (pointer && pointer.id === event.pointerId) pointer = null;
      target.classList.remove('dragging');
    }
    function wheel(event) {
      event.preventDefault();
      view.distance = Math.max(1.15, Math.min(9, view.distance * Math.exp(event.deltaY * .001)));
    }
    target.addEventListener('pointerdown', pointerDown);
    target.addEventListener('pointermove', pointerMove);
    target.addEventListener('pointerup', release);
    target.addEventListener('pointercancel', release);
    target.addEventListener('wheel', wheel, { passive: false });
    target.addEventListener('dblclick', reset);
    document.getElementById('pointSize').oninput = event => { view.pointSize = Number(event.target.value); };
    document.getElementById('resetView').onclick = reset;
    const liveToggle = document.getElementById('liveToggle');
    liveToggle.disabled = !liveAvailable;
    liveToggle.setAttribute('aria-pressed', String(view.live));
    liveToggle.textContent = !liveAvailable
      ? 'Live unavailable' : view.live ? 'Live colour · connecting' : 'Live colour · off';
    liveToggle.onclick = () => {
      if (!liveAvailable) return;
      view.live = !view.live;
      liveToggle.setAttribute('aria-pressed', String(view.live));
      liveToggle.textContent = view.live
        ? (atlasReady ? 'Live colour · on' : 'Live colour · connecting')
        : 'Live colour · off';
    };
    const orbitToggle = document.getElementById('orbitToggle');
    orbitToggle.textContent = view.orbiting ? 'Orbiting' : 'Orbit paused';
    orbitToggle.setAttribute('aria-pressed', String(view.orbiting));
    orbitToggle.onclick = event => {
      view.orbiting = !view.orbiting;
      event.currentTarget.textContent = view.orbiting ? 'Orbiting' : 'Orbit paused';
      event.currentTarget.setAttribute('aria-pressed', String(view.orbiting));
    };
    document.getElementById('pointSize').value = String(view.pointSize);
    animation = requestAnimationFrame(draw);
    return {
      snapshot() {
        return { ...view };
      },
      destroy() {
        cancelAnimationFrame(animation);
        target.removeEventListener('pointerdown', pointerDown);
        target.removeEventListener('pointermove', pointerMove);
        target.removeEventListener('pointerup', release);
        target.removeEventListener('pointercancel', release);
        target.removeEventListener('wheel', wheel);
        target.removeEventListener('dblclick', reset);
        liveResources.hls.forEach(hls => { try { hls.destroy(); } catch {} });
        liveResources.peerConnections.forEach(pc => { try { pc.close(); } catch {} });
        liveResources.videos.forEach(video => {
          try { video.pause(); video.srcObject = null; video.removeAttribute('src'); video.load(); video.remove(); } catch {}
        });
        gl.deleteTexture(liveTexture);
        gl.deleteBuffer(positionBuffer); gl.deleteBuffer(colorBuffer);
        gl.deleteBuffer(liveUvBuffer); gl.deleteBuffer(cameraBuffer);
        if (indexBuffer) gl.deleteBuffer(indexBuffer);
        gl.deleteProgram(program);
      },
    };
  }

  function setArtifactView(viewName) {
    document.querySelectorAll('[data-view]').forEach(item => {
      const selected = item.dataset.view === viewName;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-pressed', String(selected));
    });
    canvas.hidden = viewName !== 'cloud';
    depthImage.hidden = viewName !== 'depth';
    fallbackImage.hidden = viewName !== 'overview';
    document.querySelector('.sp-stage-bottom').hidden = viewName !== 'cloud';
  }

  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => {
    setArtifactView(button.dataset.view);
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
    state.scene = null; state.run = null; state.pendingRun = null;
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
    refreshButton.disabled = true;
    densitySelect.disabled = true;
    refreshButton.textContent = 'Refresh geometry';
    refreshMessage.hidden = true;
  }

  async function refreshGeometry() {
    if (!state.scene || !state.run?.artifacts?.point_cloud || state.pendingRun) return;
    const sceneId = state.scene.id;
    const densityPreset = densitySelect.value;
    const densityLabel = densitySelect.options[densitySelect.selectedIndex].textContent;
    refreshButton.disabled = true;
    densitySelect.disabled = true;
    refreshButton.textContent = 'Queuing geometry…';
    refreshMessage.hidden = false;
    refreshMessage.className = 'sp-refresh-message busy';
    refreshMessage.textContent = `Requesting ${densityLabel.toLowerCase()}. The current model remains active.`;
    try {
      const response = await fetch(
        '/api/spatial/scenes/' + encodeURIComponent(sceneId) + '/runs',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ density_preset: densityPreset }),
        },
      );
      if (!response.ok) throw await responseError(response, 'Unable to refresh geometry');
      const payload = await response.json();
      renderRefreshState(payload.run || { status: 'queued', created_at: new Date().toISOString() });
      await refreshScenes();
      const updated = state.scenes.find(scene => scene.id === sceneId);
      if (updated && state.scene?.id === sceneId) {
        sceneSelect.value = sceneId;
        await openScene(updated);
      }
    } catch (error) {
      console.error(error);
      renderRefreshState(null, { error: error.message });
    }
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
  refreshButton.addEventListener('click', refreshGeometry);
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
