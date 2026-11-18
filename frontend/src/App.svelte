<script>
  import { onMount } from 'svelte';

  let graph = $state(null);
  let loading = $state(true);
  let error = $state('');

  let viewMode = $state('symbols'); // 'symbols' | 'files' | 'folders'
  let sidebarTab = $state('hubs'); // 'hubs' | 'cycles' | 'dead' | 'impact'
  let search = $state('');
  let langFilter = $state('all');
  let focusOnly = $state(false);
  let selectedId = $state(null);
  let pathTargetId = $state('');
  let highlightedCycle = $state(null);

  let impactData = $state(null);
  let impactLoading = $state(false);

  let snippet = $state(null);
  let snippetLoading = $state(false);

  // Pan & zoom state
  let pan = $state({ x: 0, y: 0 });
  let zoom = $state(1);
  let draggingCanvas = $state(false);
  let draggingNode = $state(null);
  let dragStart = { x: 0, y: 0 };
  let pointerDownPos = { x: 0, y: 0 };
  let didDrag = false;
  let canvasEl = $state(null);
  let svgEl = $state(null);

  let positions = $state({});

  const LANG_COLORS = {
    python: '#3b82f6',
    javascript: '#eab308',
    typescript: '#06b6d4',
    svelte: '#f97316',
    go: '#14b8a6',
    rust: '#ef4444',
    java: '#a855f7',
    c: '#64748b',
    cpp: '#ec4899',
  };

  async function loadGraph(rescan = false) {
    loading = true;
    error = '';
    try {
      const res = await fetch(rescan ? '/api/graph?rescan=1' : '/api/graph');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      graph = data;
      impactData = null;
      recomputeLayout();
      if (selectedId && !data.nodes.some((n) => n.id === selectedId)) {
        selectedId = null;
        snippet = null;
      }
    } catch (err) {
      error = err.message || 'Failed to load graph';
    } finally {
      loading = false;
    }
  }

  function buildFileNodesAndEdges(data) {
    const fileIn = {};
    const fileOut = {};
    for (const e of data.file_edges || []) {
      fileOut[e.source] = (fileOut[e.source] || 0) + 1;
      fileIn[e.target] = (fileIn[e.target] || 0) + 1;
    }
    const nodes = data.files.map((f) => ({
      id: f.path,
      name: f.path.split('/').pop(),
      kind: 'file',
      file: f.path,
      language: f.language,
      start_line: 1,
      end_line: Math.min(f.lines, 120),
      docstring: `${f.lines} lines · ${f.symbol_count} symbols`,
      fan_in: fileIn[f.path] || 0,
      fan_out: fileOut[f.path] || 0,
    }));
    return { nodes, edges: data.file_edges || [] };
  }

  async function loadImpact(opts = {}) {
    if (!graph) return;
    impactLoading = true;
    try {
      const params = new URLSearchParams();
      if (opts.file) {
        params.set('file', opts.file);
        params.set('git', '0');
      } else if (opts.symbol) {
        params.set('symbol', opts.symbol);
        params.set('git', '0');
      } else {
        params.set('git', '1');
      }
      const res = await fetch(`/api/impact?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      impactData = await res.json();
      sidebarTab = 'impact';
      if (viewMode === 'folders') {
        viewMode = 'symbols';
        recomputeLayout();
      }
    } catch {
      impactData = null;
    } finally {
      impactLoading = false;
    }
  }

  function folderOf(filePath) {
    const idx = filePath.lastIndexOf('/');
    return idx === -1 ? '(root)' : filePath.slice(0, idx);
  }

  function buildFolderNodesAndEdges(data) {
    const groups = new Map();
    for (const f of data.files) {
      const dir = folderOf(f.path);
      let g = groups.get(dir);
      if (!g) {
        g = {
          id: dir,
          name: dir,
          kind: 'folder',
          file: f.path,
          fileCount: 0,
          symbolCount: 0,
          langCounts: {},
        };
        groups.set(dir, g);
      }
      g.fileCount += 1;
      g.symbolCount += f.symbol_count || 0;
      g.langCounts[f.language] = (g.langCounts[f.language] || 0) + 1;
    }

    const edgeMap = new Map();
    const folderIn = {};
    const folderOut = {};
    for (const e of data.file_edges || []) {
      const sf = folderOf(e.source);
      const tf = folderOf(e.target);
      if (sf === tf) continue;
      const key = `${sf}->${tf}`;
      edgeMap.set(key, (edgeMap.get(key) || 0) + (e.call_count || 1));
    }

    const edges = [];
    for (const [key, cnt] of edgeMap.entries()) {
      const [source, target] = key.split('->');
      edges.push({ source, target, call_count: cnt });
      folderOut[source] = (folderOut[source] || 0) + 1;
      folderIn[target] = (folderIn[target] || 0) + 1;
    }

    const nodes = [...groups.values()].map((g) => {
      const domLang = Object.entries(g.langCounts).sort((a, b) => b[1] - a[1])[0]?.[0] || 'python';
      return {
        id: g.id,
        name: g.name,
        kind: 'folder',
        file: g.file,
        language: domLang,
        start_line: 1,
        end_line: 1,
        docstring: `${g.fileCount} files · ${g.symbolCount} symbols`,
        fan_in: folderIn[g.id] || 0,
        fan_out: folderOut[g.id] || 0,
      };
    });

    return { nodes, edges };
  }

  function recomputeLayout() {
    if (!graph) return;
    if (viewMode === 'files') {
      const { nodes, edges } = buildFileNodesAndEdges(graph);
      layoutNodes(nodes, edges);
    } else if (viewMode === 'folders') {
      const { nodes, edges } = buildFolderNodesAndEdges(graph);
      layoutNodes(nodes, edges);
    } else {
      const sorted = [...graph.nodes].sort(
        (a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out)
      );
      layoutNodes(sorted, graph.edges);
    }
  }

  function fitToView(nodes, posMap = positions) {
    if (!canvasEl || !nodes || nodes.length === 0) return;
    const width = canvasEl.clientWidth || 900;
    const height = canvasEl.clientHeight || 600;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const n of nodes) {
      const p = posMap[n.id];
      if (!p) continue;
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
    if (!Number.isFinite(minX)) return;
    const pad = 90;
    const spanX = Math.max(maxX - minX + pad * 2, 240);
    const spanY = Math.max(maxY - minY + pad * 2, 240);
    const nextZoom = Math.max(0.25, Math.min(1.25, Math.min(width / spanX, height / spanY)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    zoom = nextZoom;
    pan = {
      x: width / 2 - cx * nextZoom,
      y: height / 2 - cy * nextZoom,
    };
  }

  function layoutNodes(nodes, edges) {
    const pos = {};
    const count = Math.max(nodes.length, 1);
    const cols = Math.ceil(Math.sqrt(count));
    const spacing = 165;

    nodes.forEach((n, idx) => {
      const col = idx % cols;
      const row = Math.floor(idx / cols);
      pos[n.id] = {
        x: (col - cols / 2) * spacing + 480,
        y: (row - cols / 2) * spacing + 320,
      };
    });

    const simNodes = nodes.slice(0, 220);
    const simIds = new Set(simNodes.map((n) => n.id));
    const simEdges = edges.filter((e) => simIds.has(e.source) && simIds.has(e.target));
    const maxIters = simNodes.length > 140 ? 28 : 42;
    const cellSize = 340;
    const useGrid = simNodes.length > 75;

    for (let iter = 0; iter < maxIters; iter++) {
      const disp = {};
      for (const n of simNodes) disp[n.id] = { x: 0, y: 0 };

      if (useGrid) {
        const grid = new Map();
        for (const n of simNodes) {
          const p = pos[n.id];
          const cx = Math.floor(p.x / cellSize);
          const cy = Math.floor(p.y / cellSize);
          const key = `${cx},${cy}`;
          let bucket = grid.get(key);
          if (!bucket) {
            bucket = [];
            grid.set(key, bucket);
          }
          bucket.push(n.id);
        }
        for (const n of simNodes) {
          const a = n.id;
          const pa = pos[a];
          const cx = Math.floor(pa.x / cellSize);
          const cy = Math.floor(pa.y / cellSize);
          for (let ox = -1; ox <= 1; ox++) {
            for (let oy = -1; oy <= 1; oy++) {
              const bucket = grid.get(`${cx + ox},${cy + oy}`);
              if (!bucket) continue;
              for (const b of bucket) {
                if (a >= b) continue;
                const dx = pa.x - pos[b].x;
                const dy = pa.y - pos[b].y;
                const dist2 = dx * dx + dy * dy + 0.01;
                if (dist2 < 110000) {
                  const dist = Math.sqrt(dist2);
                  const force = 4400 / dist2;
                  const fx = (dx / dist) * force * 18;
                  const fy = (dy / dist) * force * 18;
                  disp[a].x += fx;
                  disp[a].y += fy;
                  disp[b].x -= fx;
                  disp[b].y -= fy;
                }
              }
            }
          }
        }
      } else {
        for (let i = 0; i < simNodes.length; i++) {
          for (let j = i + 1; j < simNodes.length; j++) {
            const a = simNodes[i].id;
            const b = simNodes[j].id;
            const dx = pos[a].x - pos[b].x;
            const dy = pos[a].y - pos[b].y;
            const dist2 = dx * dx + dy * dy + 0.01;
            if (dist2 < 110000) {
              const dist = Math.sqrt(dist2);
              const force = 4400 / dist2;
              const fx = (dx / dist) * force * 18;
              const fy = (dy / dist) * force * 18;
              disp[a].x += fx;
              disp[a].y += fy;
              disp[b].x -= fx;
              disp[b].y -= fy;
            }
          }
        }
      }

      for (const e of simEdges) {
        const dx = pos[e.target].x - pos[e.source].x;
        const dy = pos[e.target].y - pos[e.source].y;
        const dist = Math.sqrt(dx * dx + dy * dy) + 0.01;
        const force = (dist - 155) * 0.06;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        disp[e.source].x += fx;
        disp[e.source].y += fy;
        disp[e.target].x -= fx;
        disp[e.target].y -= fy;
      }

      let maxStep = 0;
      for (const n of simNodes) {
        const sx = Math.max(-24, Math.min(24, disp[n.id].x));
        const sy = Math.max(-24, Math.min(24, disp[n.id].y));
        pos[n.id].x += sx;
        pos[n.id].y += sy;
        const step = Math.abs(sx) + Math.abs(sy);
        if (step > maxStep) maxStep = step;
      }
      if (maxStep < 0.8) break;
    }

    positions = pos;
    requestAnimationFrame(() => fitToView(nodes.slice(0, 250), pos));
  }

  async function selectNode(node) {
    selectedId = node.id;
    pathTargetId = '';
    highlightedCycle = null;
    snippet = null;
    if (node.kind === 'folder') {
      snippetLoading = false;
      return;
    }
    snippetLoading = true;
    try {
      const params = new URLSearchParams({
        path: node.file,
        start: String(node.start_line),
        end: String(node.end_line),
      });
      const res = await fetch(`/api/file?${params.toString()}`);
      if (res.ok && selectedId === node.id) {
        snippet = await res.json();
      }
    } catch {
      if (selectedId === node.id) snippet = null;
    } finally {
      if (selectedId === node.id) snippetLoading = false;
    }
  }

  function switchView(mode) {
    viewMode = mode;
    selectedId = null;
    pathTargetId = '';
    focusOnly = false;
    highlightedCycle = null;
    snippet = null;
    recomputeLayout();
  }

  let activeData = $derived.by(() => {
    if (!graph) return { nodes: [], edges: [] };
    if (viewMode === 'files') return buildFileNodesAndEdges(graph);
    if (viewMode === 'folders') return buildFolderNodesAndEdges(graph);
    return { nodes: graph.nodes, edges: graph.edges };
  });

  let adjMaps = $derived.by(() => {
    const nodeMap = new Map();
    const outMap = new Map();
    const inMap = new Map();
    for (const n of activeData.nodes) {
      nodeMap.set(n.id, n);
    }
    for (const e of activeData.edges) {
      if (!outMap.has(e.source)) outMap.set(e.source, []);
      outMap.get(e.source).push(e.target);
      if (!inMap.has(e.target)) inMap.set(e.target, []);
      inMap.get(e.target).push(e.source);
    }
    return { nodeMap, outMap, inMap };
  });

  let impactSeedSet = $derived.by(() => {
    if (!impactData) return new Set();
    if (viewMode === 'files') return new Set(impactData.changed_files || []);
    if (viewMode === 'symbols') return new Set((impactData.changed_symbols || []).map((s) => s.id));
    return new Set();
  });

  let impactUpstreamSet = $derived.by(() => {
    if (!impactData) return new Set();
    if (viewMode === 'files') {
      const seeds = impactSeedSet;
      return new Set((impactData.impacted_files || []).filter((f) => !seeds.has(f)));
    }
    if (viewMode === 'symbols') return new Set((impactData.impacted_symbols || []).map((s) => s.id));
    return new Set();
  });

  let visibleNodes = $derived.by(() => {
    const q = search.trim().toLowerCase();
    let base = activeData.nodes.filter((n) => {
      if (langFilter !== 'all' && n.language !== langFilter) return false;
      if (q && !n.name.toLowerCase().includes(q) && !n.file.toLowerCase().includes(q)) {
        return false;
      }
      return true;
    });

    if (focusOnly && selectedId) {
      const neighborIds = new Set([selectedId]);
      for (const tgt of adjMaps.outMap.get(selectedId) || []) neighborIds.add(tgt);
      for (const src of adjMaps.inMap.get(selectedId) || []) neighborIds.add(src);
      base = base.filter((n) => neighborIds.has(n.id));
    }

    // Prioritize highest-connectivity hubs and always keep selected/cycle/impact nodes visible
    const sorted = [...base]
      .sort((a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out));
    const capped = sorted.slice(0, 250);
    const cappedIds = new Set(capped.map((n) => n.id));
    for (const extra of activeData.nodes) {
      if (
        !cappedIds.has(extra.id) &&
        (extra.id === selectedId ||
          cycleNodeSet.has(extra.id) ||
          impactSeedSet.has(extra.id) ||
          impactUpstreamSet.has(extra.id))
      ) {
        capped.push(extra);
        cappedIds.add(extra.id);
      }
    }
    return capped;
  });

  let visibleIds = $derived(new Set(visibleNodes.map((n) => n.id)));

  let visibleEdges = $derived.by(() =>
    activeData.edges.filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
  );

  let selectedNode = $derived(
    adjMaps.nodeMap.get(selectedId) ?? null
  );

  let selectedCallers = $derived.by(() => {
    if (!selectedId) return [];
    const ids = new Set(adjMaps.inMap.get(selectedId) || []);
    return [...ids].map((id) => adjMaps.nodeMap.get(id)).filter(Boolean);
  });

  let selectedCallees = $derived.by(() => {
    if (!selectedId) return [];
    const ids = new Set(adjMaps.outMap.get(selectedId) || []);
    return [...ids].map((id) => adjMaps.nodeMap.get(id)).filter(Boolean);
  });

  let deadSymbols = $derived.by(() => {
    if (!graph) return [];
    return graph.nodes.filter(
      (n) => n.kind !== 'class' && n.fan_in === 0 && (langFilter === 'all' || n.language === langFilter)
    );
  });

  let filteredHubs = $derived.by(() => {
    if (!graph) return [];
    if (viewMode === 'symbols') {
      if (langFilter === 'all') return graph.stats.hotspots;
      return graph.nodes
        .filter((n) => n.language === langFilter && (n.fan_in + n.fan_out) > 0)
        .sort((a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out))
        .slice(0, 10);
    }
    return activeData.nodes
      .filter((n) => langFilter === 'all' || n.language === langFilter)
      .sort((a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out))
      .slice(0, 10);
  });

  // Shortest directed call path from selectedId -> pathTargetId via BFS
  let shortestPath = $derived.by(() => {
    if (!selectedId || !pathTargetId || selectedId === pathTargetId) return null;
    const queue = [[selectedId]];
    let head = 0;
    const visited = new Set([selectedId]);
    while (head < queue.length) {
      const path = queue[head++];
      const tail = path[path.length - 1];
      if (tail === pathTargetId) return path;
      for (const next of adjMaps.outMap.get(tail) || []) {
        if (!visited.has(next)) {
          visited.add(next);
          queue.push([...path, next]);
        }
      }
    }
    return [];
  });

  let pathEdgeSet = $derived.by(() => {
    const s = new Set();
    if (shortestPath && shortestPath.length > 1) {
      for (let i = 0; i < shortestPath.length - 1; i++) {
        s.add(`${shortestPath[i]}->${shortestPath[i + 1]}`);
      }
    }
    return s;
  });

  let pathOptions = $derived.by(() => {
    if (!selectedId) return [];
    const reachable = new Set();
    const q = [selectedId];
    let head = 0;
    while (head < q.length && reachable.size < 200) {
      const cur = q[head++];
      for (const nxt of adjMaps.outMap.get(cur) || []) {
        if (nxt !== selectedId && !reachable.has(nxt)) {
          reachable.add(nxt);
          q.push(nxt);
        }
      }
    }
    const items = [...reachable].map((id) => adjMaps.nodeMap.get(id)).filter(Boolean);
    if (items.length < 250) {
      const topRest = [...activeData.nodes]
        .filter((n) => n.id !== selectedId && !reachable.has(n.id))
        .sort((a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out))
        .slice(0, 250 - items.length);
      items.push(...topRest);
    }
    return items;
  });

  let cycleNodeSet = $derived(new Set(highlightedCycle || []));

  function resetView() {
    search = '';
    langFilter = 'all';
    focusOnly = false;
    highlightedCycle = null;
    impactData = null;
    if (sidebarTab === 'impact') sidebarTab = 'hubs';
    fitToView(visibleNodes);
  }

  function onWheel(e) {
    e.preventDefault();
    if (!canvasEl) return;
    const rect = canvasEl.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    const nextZoom = Math.max(0.2, Math.min(3.5, zoom * factor));
    const scale = nextZoom / zoom;
    pan = {
      x: mx - (mx - pan.x) * scale,
      y: my - (my - pan.y) * scale,
    };
    zoom = nextZoom;
  }

  function onCanvasPointerDown(e) {
    if (e.target.closest('[data-node-id]')) return;
    draggingCanvas = true;
    didDrag = false;
    pointerDownPos = { x: e.clientX, y: e.clientY };
    dragStart = { x: e.clientX - pan.x, y: e.clientY - pan.y };
  }

  function onNodePointerDown(e, nodeId) {
    e.stopPropagation();
    draggingNode = nodeId;
    didDrag = false;
    pointerDownPos = { x: e.clientX, y: e.clientY };
    dragStart = { x: e.clientX, y: e.clientY };
  }

  function onPointerMove(e) {
    if (draggingCanvas || draggingNode) {
      const dist = Math.hypot(e.clientX - pointerDownPos.x, e.clientY - pointerDownPos.y);
      if (dist > 4) didDrag = true;
    }
    if (draggingCanvas) {
      pan = { x: e.clientX - dragStart.x, y: e.clientY - dragStart.y };
    } else if (draggingNode) {
      const dx = (e.clientX - dragStart.x) / zoom;
      const dy = (e.clientY - dragStart.y) / zoom;
      dragStart = { x: e.clientX, y: e.clientY };
      const cur = positions[draggingNode];
      if (cur) {
        positions = {
          ...positions,
          [draggingNode]: { x: cur.x + dx, y: cur.y + dy },
        };
      }
    }
  }

  function centerOnNode(nodeId) {
    if (!canvasEl || !positions[nodeId]) return;
    const rect = canvasEl.getBoundingClientRect();
    const p = positions[nodeId];
    pan = {
      x: rect.width / 2 - p.x * zoom,
      y: rect.height / 2 - p.y * zoom,
    };
  }

  function onPointerUp() {
    if (draggingCanvas && !didDrag) {
      selectedId = null;
      pathTargetId = '';
      highlightedCycle = null;
      snippet = null;
    }
    draggingCanvas = false;
    draggingNode = null;
  }

  function exportSvg() {
    if (!svgEl) return;
    const clone = svgEl.cloneNode(true);
    const width = svgEl.clientWidth || 1200;
    const height = svgEl.clientHeight || 800;
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('width', String(width));
    clone.setAttribute('height', String(height));
    clone.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    bg.setAttribute('width', '100%');
    bg.setAttribute('height', '100%');
    bg.setAttribute('fill', '#020617');
    clone.insertBefore(bg, clone.firstChild);
    for (const label of clone.querySelectorAll('.node-label')) {
      label.setAttribute('fill', '#cbd5e1');
      label.setAttribute('font-size', '11px');
      label.setAttribute('font-family', 'Inter, ui-sans-serif, system-ui, sans-serif');
    }
    const serializer = new XMLSerializer();
    const raw = serializer.serializeToString(clone);
    const blob = new Blob([raw], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `codeatlas-${viewMode}.svg`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function exportMermaid() {
    const safe = (id) => 'n_' + id.replace(/[^a-zA-Z0-9]/g, '_');
    const lines = ['flowchart LR'];
    const nodeMap = new Map(visibleNodes.map((n) => [n.id, n]));
    const emitted = new Set();
    for (const e of visibleEdges.slice(0, 100)) {
      const s = nodeMap.get(e.source);
      const t = nodeMap.get(e.target);
      if (!s || !t) continue;
      for (const n of [s, t]) {
        if (!emitted.has(n.id)) {
          emitted.add(n.id);
          const cleanName = n.name.replace(/"/g, "'");
          lines.push(`  ${safe(n.id)}["${cleanName}"]`);
        }
      }
      lines.push(`  ${safe(s.id)} --> ${safe(t.id)}`);
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `codeatlas-${viewMode}.mmd`;
    a.click();
    URL.revokeObjectURL(url);
  }

  onMount(() => {
    loadGraph(false);
  });
</script>

<svelte:window onpointermove={onPointerMove} onpointerup={onPointerUp} />

<div class="shell">
  <header class="topbar">
    <div class="brand">
      <span class="logo">◈</span>
      <div>
        <strong>CodeAtlas</strong>
        {#if graph}
          <span class="root-path">{graph.root}</span>
        {/if}
      </div>
    </div>

    {#if graph}
      <div class="stats">
        <span>{graph.stats.total_files} files</span>
        <span>{graph.stats.total_symbols} symbols</span>
        <span>{graph.stats.total_edges} edges</span>
        {#if graph.stats.cycle_count > 0}
          <span class="warn">{graph.stats.cycle_count} cycles</span>
        {/if}
        {#if graph.stats.health_score !== undefined}
          <span class="health">Health {graph.stats.health_score}/100</span>
        {/if}
      </div>
    {/if}

    <div class="controls">
      <div class="seg">
        <button class:seg-active={viewMode === 'symbols'} onclick={() => switchView('symbols')}>
          Symbols
        </button>
        <button class:seg-active={viewMode === 'files'} onclick={() => switchView('files')}>
          Files
        </button>
        <button class:seg-active={viewMode === 'folders'} onclick={() => switchView('folders')}>
          Folders
        </button>
      </div>
      <input
        type="search"
        placeholder="Search symbol or file..."
        bind:value={search}
      />
      <select bind:value={langFilter}>
        <option value="all">All Languages</option>
        <option value="python">Python</option>
        <option value="javascript">JavaScript</option>
        <option value="typescript">TypeScript</option>
        <option value="svelte">Svelte</option>
        <option value="go">Go</option>
        <option value="rust">Rust</option>
        <option value="java">Java</option>
        <option value="c">C</option>
        <option value="cpp">C++</option>
      </select>
      <label class="toggle">
        <input
          type="checkbox"
          bind:checked={focusOnly}
          disabled={!selectedId}
          onchange={() => { if (focusOnly && selectedId) centerOnNode(selectedId); }}
        />
        Focus 1-hop
      </label>
      <button
        class:impact-btn={Boolean(impactData)}
        onclick={() => loadImpact({ git: true })}
        disabled={impactLoading}
        title="Show git diff blast radius"
      >
        {impactLoading ? 'Tracing...' : 'Impact'}
      </button>
      <button onclick={resetView} title="Reset pan, zoom, and filters">
        Reset
      </button>
      <button onclick={exportMermaid} disabled={!visibleEdges.length} title="Download Mermaid diagram">
        .mmd
      </button>
      <button onclick={exportSvg} disabled={!visibleNodes.length} title="Download SVG">
        .svg
      </button>
      <button onclick={() => loadGraph(true)} disabled={loading}>
        {loading ? 'Scanning...' : 'Rescan'}
      </button>
    </div>
  </header>

  <main class="workspace">
    <aside class="sidebar">
      {#if graph}
        <div class="tabs">
          <button class:tab-active={sidebarTab === 'hubs'} onclick={() => (sidebarTab = 'hubs')}>
            Hubs
          </button>
          <button class:tab-active={sidebarTab === 'cycles'} onclick={() => (sidebarTab = 'cycles')}>
            Cycles ({graph.cycles.length})
          </button>
          <button class:tab-active={sidebarTab === 'dead'} onclick={() => (sidebarTab = 'dead')}>
            Dead ({deadSymbols.length})
          </button>
          <button class:tab-active={sidebarTab === 'impact'} onclick={() => (sidebarTab = 'impact')}>
            Impact
          </button>
        </div>

        {#if sidebarTab === 'hubs'}
          <section>
            <h3>{viewMode === 'folders' ? 'Folder Clusters' : viewMode === 'files' ? 'Top File Hubs' : 'Top Hubs'}</h3>
            <ul class="list">
              {#each filteredHubs as hub}
                <li>
                  <button
                    class="list-btn"
                    class:active={selectedId === hub.id}
                    onclick={() => {
                      const found = activeData.nodes.find((n) => n.id === hub.id) || graph.nodes.find((n) => n.id === hub.id);
                      if (found) selectNode(found);
                    }}
                  >
                    <span class="sym-name">{hub.name}</span>
                    <span class="meta">in:{hub.fan_in} out:{hub.fan_out}</span>
                  </button>
                </li>
              {:else}
                <li class="muted">No call hubs detected.</li>
              {/each}
            </ul>
          </section>
        {:else if sidebarTab === 'cycles'}
          <section>
            <h3>Circular Dependencies</h3>
            <ul class="list">
              {#each graph.cycles as cyc, idx}
                <li>
                  <button
                    class="list-btn"
                    class:active={highlightedCycle === cyc}
                    onclick={() => {
                      if (viewMode !== 'symbols') switchView('symbols');
                      search = '';
                      langFilter = 'all';
                      focusOnly = false;
                      highlightedCycle = cyc;
                      selectedId = null;
                      pathTargetId = '';
                    }}
                  >
                    <span class="sym-name">Cycle #{idx + 1} ({cyc.length})</span>
                    <span class="meta">{cyc.map((id) => id.split('::').pop()).join(' ↔ ')}</span>
                  </button>
                </li>
              {:else}
                <li class="muted">No circular call cycles detected.</li>
              {/each}
            </ul>
          </section>
        {:else if sidebarTab === 'dead'}
          <section>
            <h3>Unreferenced (fan-in = 0)</h3>
            <ul class="list">
              {#each deadSymbols.slice(0, 80) as sym}
                <li>
                  <button
                    class="list-btn"
                    class:active={selectedId === sym.id}
                    onclick={() => {
                      if (viewMode !== 'symbols') switchView('symbols');
                      selectNode(sym);
                    }}
                  >
                    <span class="sym-name">{sym.name}</span>
                    <span class="meta">{sym.file.split('/').pop()}</span>
                  </button>
                </li>
              {:else}
                <li class="muted">All functions are referenced.</li>
              {/each}
            </ul>
          </section>
        {:else}
          <section>
            <div class="impact-hdr">
              <h3>Blast Radius</h3>
              {#if impactData}
                <button class="clear-btn" onclick={() => (impactData = null)}>Clear</button>
              {/if}
            </div>
            {#if !impactData}
              <p class="muted">Click "Impact" in the top bar for git diff, or "Blast Radius" on any node.</p>
            {:else}
              <p class="impact-meta">
                {impactData.source} · {impactData.summary.changed_symbol_count} changed ·
                {impactData.summary.impacted_symbol_count} upstream ·
                {impactData.summary.entrypoint_count} entrypoints
              </p>
              {#if impactData.affected_entrypoints?.length}
                <h4>Entrypoints ({impactData.affected_entrypoints.length})</h4>
                <ul class="list">
                  {#each impactData.affected_entrypoints.slice(0, 20) as ep}
                    <li>
                      <button class="list-btn" onclick={() => {
                        if (viewMode !== 'symbols') switchView('symbols');
                        const found = graph.nodes.find((n) => n.id === ep.id);
                        if (found) selectNode(found);
                      }}>
                        <span class="sym-name">{ep.name}</span>
                        <span class="meta">hop {ep.hop}</span>
                      </button>
                    </li>
                  {/each}
                </ul>
              {/if}
              <h4>Upstream Callers ({impactData.impacted_symbols?.length || 0})</h4>
              <ul class="list">
                {#each (impactData.impacted_symbols || []).slice(0, 40) as sym}
                  <li>
                    <button class="list-btn" onclick={() => {
                      if (viewMode !== 'symbols') switchView('symbols');
                      const found = graph.nodes.find((n) => n.id === sym.id);
                      if (found) selectNode(found);
                    }}>
                      <span class="sym-name">{sym.name}</span>
                      <span class="meta">hop {sym.hop}</span>
                    </button>
                  </li>
                {:else}
                  <li class="muted">No upstream callers found.</li>
                {/each}
              </ul>
            {/if}
          </section>
        {/if}

        <section>
          <h3>Visible ({visibleNodes.length})</h3>
          <ul class="list">
            {#each visibleNodes.slice(0, 80) as node}
              <li>
                <button
                  class="list-btn"
                  class:active={selectedId === node.id}
                  onclick={() => selectNode(node)}
                >
                  <span class="dot" style:background={LANG_COLORS[node.language] || '#94a3b8'}></span>
                  <span class="sym-name">{node.name}</span>
                  <span class="meta">{node.kind === 'folder' ? node.docstring : node.file.split('/').pop()}</span>
                </button>
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </aside>

    <div
      class="canvas-wrap"
      role="application"
      aria-label="Interactive call graph canvas"
      bind:this={canvasEl}
      onwheel={onWheel}
      onpointerdown={onCanvasPointerDown}
    >
      {#if loading && !graph}
        <div class="center-msg">Scanning repository...</div>
      {:else if error}
        <div class="center-msg err">{error}</div>
      {:else if visibleNodes.length === 0}
        <div class="center-msg">No matching symbols found.</div>
      {:else}
        <svg class="graph-svg" bind:this={svgEl}>
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 10 10"
              refX="18"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#64748b" />
            </marker>
            <marker
              id="arrow-active"
              viewBox="0 0 10 10"
              refX="18"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#38bdf8" />
            </marker>
            <marker
              id="arrow-path"
              viewBox="0 0 10 10"
              refX="18"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#22c55e" />
            </marker>
            <marker
              id="arrow-cycle"
              viewBox="0 0 10 10"
              refX="18"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#f97316" />
            </marker>
            <marker
              id="arrow-impact"
              viewBox="0 0 10 10"
              refX="18"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#ef4444" />
            </marker>
          </defs>

          <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
            {#each visibleEdges as edge}
              {@const a = positions[edge.source]}
              {@const b = positions[edge.target]}
              {#if a && b}
                {@const inPath = pathEdgeSet.has(`${edge.source}->${edge.target}`)}
                {@const inCycle = cycleNodeSet.has(edge.source) && cycleNodeSet.has(edge.target)}
                {@const inImpact = (impactSeedSet.has(edge.source) || impactUpstreamSet.has(edge.source)) && (impactSeedSet.has(edge.target) || impactUpstreamSet.has(edge.target))}
                {@const active = inPath || inCycle || inImpact || selectedId === edge.source || selectedId === edge.target}
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={inPath ? '#22c55e' : inImpact ? '#ef4444' : inCycle ? '#f97316' : active ? '#38bdf8' : '#334155'}
                  stroke-width={inPath || inCycle || inImpact ? 2.8 : active ? 2.2 : 1.2}
                  stroke-opacity={(selectedId || highlightedCycle || impactData) && !active ? 0.18 : 0.88}
                  marker-end={inPath ? 'url(#arrow-path)' : inImpact ? 'url(#arrow-impact)' : inCycle ? 'url(#arrow-cycle)' : active ? 'url(#arrow-active)' : 'url(#arrow)'}
                />
              {/if}
            {/each}

            {#each visibleNodes as node}
              {@const p = positions[node.id] || { x: 0, y: 0 }}
              {@const isSel = selectedId === node.id}
              {@const inCycle = cycleNodeSet.has(node.id)}
              {@const isSeed = impactSeedSet.has(node.id)}
              {@const isUp = impactUpstreamSet.has(node.id)}
              {@const color = LANG_COLORS[node.language] || '#94a3b8'}
              <g
                data-node-id={node.id}
                role="button"
                tabindex="0"
                aria-label={node.name}
                transform={`translate(${p.x}, ${p.y})`}
                class="node-group"
                onpointerdown={(e) => onNodePointerDown(e, node.id)}
                onclick={() => { if (!didDrag) selectNode(node); }}
                onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') selectNode(node); }}
              >
                <title>{node.file} :: {node.name} (L{node.start_line}-{node.end_line})</title>
                <circle
                  r={isSel ? 16 : 11 + Math.min(8, node.fan_in + node.fan_out)}
                  fill={isSeed ? '#450a0a' : isUp ? '#451a03' : '#0f172a'}
                  stroke={isSel ? '#f8fafc' : isSeed ? '#ef4444' : isUp ? '#f59e0b' : inCycle ? '#f97316' : color}
                  stroke-width={isSel || inCycle || isSeed || isUp ? 3 : 2}
                />
                <text y="26" text-anchor="middle" class="node-label">
                  {node.name.length > 22 ? node.name.slice(0, 20) + '…' : node.name}
                </text>
              </g>
            {/each}
          </g>
        </svg>
      {/if}
    </div>

    <aside class="inspector">
      {#if selectedNode}
        <div class="insp-header">
          <span class="badge" style:background={LANG_COLORS[selectedNode.language] || '#475569'}>
            {selectedNode.language}
          </span>
          <h2>{selectedNode.name}</h2>
          <p class="file-loc">{selectedNode.file}:{selectedNode.start_line}-{selectedNode.end_line}</p>
          {#if selectedNode.docstring}
            <p class="doc">{selectedNode.docstring}</p>
          {/if}
          {#if viewMode === 'files'}
            <button
              class="drill-btn"
              onclick={() => {
                const filePath = selectedNode.file;
                switchView('symbols');
                search = filePath;
              }}
            >
              Drill into file symbols →
            </button>
          {:else if viewMode === 'folders'}
            <button
              class="drill-btn"
              onclick={() => {
                const folderPath = selectedNode.id === '(root)' ? '' : selectedNode.id + '/';
                switchView('files');
                search = folderPath;
              }}
            >
              Explore folder files →
            </button>
          {/if}
          {#if viewMode !== 'folders'}
            <button
              class="drill-btn impact-action"
              onclick={() => loadImpact(viewMode === 'files' ? { file: selectedNode.file } : { symbol: selectedNode.id })}
            >
              Trace Upstream Blast Radius →
            </button>
          {/if}
        </div>

        <div class="path-box">
          <h4>Path Finder</h4>
          <select bind:value={pathTargetId} class="full-sel">
            <option value="">Select target to trace path...</option>
            {#each pathOptions as n}
              <option value={n.id}>{n.name} ({n.file.split('/').pop()})</option>
            {/each}
          </select>
          {#if shortestPath && shortestPath.length > 0}
            <p class="path-ok">
              Path ({shortestPath.length - 1} hops):
              {shortestPath.map((id) => id.split('::').pop().split('/').pop()).join(' → ')}
            </p>
          {:else if shortestPath && shortestPath.length === 0}
            <p class="muted">No directed call path found.</p>
          {/if}
        </div>

        <div class="rel-grid">
          <div>
            <h4>Callers ({selectedCallers.length})</h4>
            <div class="chips">
              {#each selectedCallers as c}
                <button class="chip in" title={c.name} onclick={() => selectNode(c)}>{c.name}</button>
              {:else}
                <span class="muted">None</span>
              {/each}
            </div>
          </div>
          <div>
            <h4>Calls ({selectedCallees.length})</h4>
            <div class="chips">
              {#each selectedCallees as c}
                <button class="chip out" title={c.name} onclick={() => selectNode(c)}>{c.name}</button>
              {:else}
                <span class="muted">None</span>
              {/each}
            </div>
          </div>
        </div>

        <div class="code-box">
          <h4>Source Preview</h4>
          {#if selectedNode.kind === 'folder'}
            <p class="muted">Folder cluster — click "Explore folder files" above to inspect files.</p>
          {:else if snippetLoading}
            <p class="muted">Loading source...</p>
          {:else if snippet}
            <pre><code>{snippet.content}</code></pre>
          {:else}
            <p class="muted">Source unavailable.</p>
          {/if}
        </div>
      {:else}
        <div class="empty-insp">
          <p>Click any node in the graph or sidebar to inspect its callers, callees, and source code.</p>
          <p class="muted">Tip: Drag nodes to rearrange, scroll to zoom at cursor, switch between Symbols &amp; Files, or trace paths between functions.</p>
        </div>
      {/if}
    </aside>
  </main>
</div>

<style>
  :global(body) {
    margin: 0;
    background: #020617;
    color: #e2e8f0;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
  }

  .shell {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    background: #0f172a;
    border-bottom: 1px solid #1e293b;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .logo {
    color: #38bdf8;
    font-size: 1.3rem;
  }

  .root-path {
    display: block;
    font-size: 0.72rem;
    color: #94a3b8;
    max-width: 145px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stats {
    display: flex;
    gap: 0.35rem;
    font-size: 0.74rem;
    flex-shrink: 0;
  }

  .stats span {
    background: #1e293b;
    padding: 0.2rem 0.45rem;
    border-radius: 999px;
    color: #cbd5e1;
    white-space: nowrap;
  }

  .stats .warn {
    background: #7c2d12;
    color: #fed7aa;
  }

  .stats .health {
    background: #14532d;
    color: #bbf7d0;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: nowrap;
    min-width: 0;
  }

  .seg,
  .tabs {
    display: flex;
    background: #020617;
    border: 1px solid #334155;
    border-radius: 6px;
    overflow: hidden;
    flex-shrink: 0;
  }

  .tabs {
    margin-bottom: 0.65rem;
  }

  .seg button,
  .tabs button {
    border: none;
    border-radius: 0;
    background: transparent;
    padding: 0.3rem 0.45rem;
    font-size: 0.74rem;
  }

  .tabs button {
    flex: 1;
  }

  .seg button.seg-active,
  .tabs button.tab-active {
    background: #1e293b;
    color: #38bdf8;
  }

  input[type='search'],
  select,
  button {
    background: #1e293b;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 0.3rem 0.48rem;
    font-size: 0.76rem;
    white-space: nowrap;
  }

  input[type='search'] {
    width: 145px;
    min-width: 90px;
  }

  button {
    cursor: pointer;
  }

  button:hover:not(:disabled) {
    border-color: #38bdf8;
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    font-size: 0.74rem;
    color: #cbd5e1;
    white-space: nowrap;
  }

  .workspace {
    display: grid;
    grid-template-columns: 250px 1fr 340px;
    flex: 1;
    min-height: 0;
  }

  .sidebar,
  .inspector {
    background: #0b1120;
    border-right: 1px solid #1e293b;
    padding: 0.75rem;
    min-width: 0;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .inspector {
    border-right: none;
    border-left: 1px solid #1e293b;
  }

  h3,
  h4 {
    margin: 0.4rem 0;
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #94a3b8;
  }

  .list {
    list-style: none;
    padding: 0;
    margin: 0 0 1rem 0;
  }

  .list-btn {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    text-align: left;
    background: transparent;
    border: none;
    padding: 0.32rem 0.4rem;
    border-radius: 5px;
    font-size: 0.8rem;
  }

  .list-btn:hover,
  .list-btn.active {
    background: #1e293b;
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .sym-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta {
    font-size: 0.7rem;
    color: #64748b;
    max-width: 95px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .canvas-wrap {
    position: relative;
    overflow: hidden;
    background: radial-gradient(circle at center, #0f172a 0%, #020617 100%);
    cursor: grab;
  }

  .graph-svg {
    width: 100%;
    height: 100%;
    display: block;
  }

  .node-group {
    cursor: pointer;
    outline: none;
  }

  .node-label {
    fill: #cbd5e1;
    font-size: 11px;
    pointer-events: none;
    user-select: none;
  }

  .center-msg {
    display: grid;
    place-items: center;
    height: 100%;
    color: #94a3b8;
  }

  .center-msg.err {
    color: #f87171;
  }

  .badge {
    display: inline-block;
    font-size: 0.7rem;
    padding: 0.15rem 0.45rem;
    border-radius: 4px;
    color: #020617;
    font-weight: 700;
    text-transform: uppercase;
  }

  .insp-header h2 {
    margin: 0.4rem 0 0.2rem;
    font-size: 1.1rem;
    overflow-wrap: anywhere;
  }

  .file-loc {
    margin: 0;
    font-size: 0.78rem;
    color: #38bdf8;
    word-break: break-all;
  }

  .doc {
    font-size: 0.82rem;
    color: #cbd5e1;
    margin-top: 0.5rem;
  }

  .drill-btn {
    margin-top: 0.5rem;
    width: 100%;
    color: #38bdf8;
  }

  .path-box {
    margin-top: 0.85rem;
  }

  .full-sel {
    width: 100%;
  }

  .path-ok {
    font-size: 0.78rem;
    color: #86efac;
    margin: 0.4rem 0 0;
    word-break: break-word;
  }

  .rel-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 0.6rem;
    margin: 1rem 0;
  }

  .rel-grid > div {
    min-width: 0;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    min-width: 0;
  }

  .chip {
    font-size: 0.75rem;
    padding: 0.2rem 0.45rem;
    border-radius: 4px;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip.in {
    border-color: #22c55e;
    color: #86efac;
  }

  .chip.out {
    border-color: #f59e0b;
    color: #fde68a;
  }

  .code-box pre {
    background: #020617;
    border: 1px solid #1e293b;
    border-radius: 6px;
    padding: 0.6rem;
    overflow-x: auto;
    font-size: 0.76rem;
    line-height: 1.45;
    max-height: 320px;
  }

  .empty-insp {
    padding: 1rem 0.25rem;
    font-size: 0.86rem;
    line-height: 1.5;
  }

  .muted {
    color: #64748b;
    font-size: 0.78rem;
  }

  .impact-btn {
    border-color: #ef4444;
    color: #fca5a5;
  }

  .impact-action {
    color: #fbbf24;
  }

  .impact-hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .clear-btn {
    padding: 0.15rem 0.45rem;
    font-size: 0.7rem;
  }

  .impact-meta {
    margin: 0.25rem 0 0.6rem;
    font-size: 0.75rem;
    color: #fbbf24;
  }
</style>
