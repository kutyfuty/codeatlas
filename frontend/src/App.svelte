<script>
  import { onMount } from 'svelte';

  let graph = $state(null);
  let loading = $state(true);
  let error = $state('');

  let viewMode = $state('symbols'); // 'symbols' | 'files'
  let sidebarTab = $state('hubs'); // 'hubs' | 'cycles' | 'dead'
  let search = $state('');
  let langFilter = $state('all');
  let focusOnly = $state(false);
  let selectedId = $state(null);
  let pathTargetId = $state('');
  let highlightedCycle = $state(null);

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
  };

  async function loadGraph(rescan = false) {
    loading = true;
    error = '';
    try {
      const res = await fetch(rescan ? '/api/graph?rescan=1' : '/api/graph');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      graph = data;
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

  function recomputeLayout() {
    if (!graph) return;
    if (viewMode === 'files') {
      const { nodes, edges } = buildFileNodesAndEdges(graph);
      layoutNodes(nodes, edges);
    } else {
      const sorted = [...graph.nodes].sort(
        (a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out)
      );
      layoutNodes(sorted, graph.edges);
    }
  }

  function layoutNodes(nodes, edges) {
    const pos = {};
    const count = Math.max(nodes.length, 1);
    const cols = Math.ceil(Math.sqrt(count));
    const spacing = 145;

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

    for (let iter = 0; iter < 45; iter++) {
      const disp = {};
      for (const n of simNodes) disp[n.id] = { x: 0, y: 0 };

      for (let i = 0; i < simNodes.length; i++) {
        for (let j = i + 1; j < simNodes.length; j++) {
          const a = simNodes[i].id;
          const b = simNodes[j].id;
          const dx = pos[a].x - pos[b].x;
          const dy = pos[a].y - pos[b].y;
          const dist2 = dx * dx + dy * dy + 0.01;
          if (dist2 < 90000) {
            const dist = Math.sqrt(dist2);
            const force = 3200 / dist2;
            const fx = (dx / dist) * force * 18;
            const fy = (dy / dist) * force * 18;
            disp[a].x += fx;
            disp[a].y += fy;
            disp[b].x -= fx;
            disp[b].y -= fy;
          }
        }
      }

      for (const e of simEdges) {
        const dx = pos[e.target].x - pos[e.source].x;
        const dy = pos[e.target].y - pos[e.source].y;
        const dist = Math.sqrt(dx * dx + dy * dy) + 0.01;
        const force = (dist - 140) * 0.06;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        disp[e.source].x += fx;
        disp[e.source].y += fy;
        disp[e.target].x -= fx;
        disp[e.target].y -= fy;
      }

      for (const n of simNodes) {
        pos[n.id].x += Math.max(-24, Math.min(24, disp[n.id].x));
        pos[n.id].y += Math.max(-24, Math.min(24, disp[n.id].y));
      }
    }

    positions = pos;
  }

  async function selectNode(node) {
    selectedId = node.id;
    pathTargetId = '';
    highlightedCycle = null;
    snippetLoading = true;
    snippet = null;
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
    highlightedCycle = null;
    snippet = null;
    recomputeLayout();
  }

  let activeData = $derived.by(() => {
    if (!graph) return { nodes: [], edges: [] };
    if (viewMode === 'files') return buildFileNodesAndEdges(graph);
    return { nodes: graph.nodes, edges: graph.edges };
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
      for (const e of activeData.edges) {
        if (e.source === selectedId) neighborIds.add(e.target);
        if (e.target === selectedId) neighborIds.add(e.source);
      }
      base = base.filter((n) => neighborIds.has(n.id));
    }

    // Prioritize highest-connectivity hubs and always keep selected/cycle nodes visible
    const sorted = [...base]
      .sort((a, b) => (b.fan_in + b.fan_out) - (a.fan_in + a.fan_out));
    const capped = sorted.slice(0, 250);
    const cappedIds = new Set(capped.map((n) => n.id));
    for (const extra of activeData.nodes) {
      if (!cappedIds.has(extra.id) && (extra.id === selectedId || cycleNodeSet.has(extra.id))) {
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
    activeData.nodes.find((n) => n.id === selectedId) ?? null
  );

  let selectedCallers = $derived.by(() => {
    if (!selectedId) return [];
    const ids = new Set(activeData.edges.filter((e) => e.target === selectedId).map((e) => e.source));
    return activeData.nodes.filter((n) => ids.has(n.id));
  });

  let selectedCallees = $derived.by(() => {
    if (!selectedId) return [];
    const ids = new Set(activeData.edges.filter((e) => e.source === selectedId).map((e) => e.target));
    return activeData.nodes.filter((n) => ids.has(n.id));
  });

  let deadSymbols = $derived.by(() => {
    if (!graph) return [];
    return graph.nodes.filter((n) => n.kind !== 'class' && n.fan_in === 0);
  });

  // Shortest directed call path from selectedId -> pathTargetId via BFS
  let shortestPath = $derived.by(() => {
    if (!selectedId || !pathTargetId || selectedId === pathTargetId) return null;
    const adj = new Map();
    for (const e of activeData.edges) {
      if (!adj.has(e.source)) adj.set(e.source, []);
      adj.get(e.source).push(e.target);
    }
    const queue = [[selectedId]];
    let head = 0;
    const visited = new Set([selectedId]);
    while (head < queue.length) {
      const path = queue[head++];
      const tail = path[path.length - 1];
      if (tail === pathTargetId) return path;
      for (const next of adj.get(tail) || []) {
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

  let cycleNodeSet = $derived(new Set(highlightedCycle || []));

  function resetView() {
    pan = { x: 0, y: 0 };
    zoom = 1;
    search = '';
    langFilter = 'all';
    focusOnly = false;
    highlightedCycle = null;
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

  function onPointerUp() {
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
      </select>
      <label class="toggle">
        <input type="checkbox" bind:checked={focusOnly} disabled={!selectedId} />
        Focus 1-hop
      </label>
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
        </div>

        {#if sidebarTab === 'hubs'}
          <section>
            <h3>Top Hubs</h3>
            <ul class="list">
              {#each graph.stats.hotspots as hub}
                <li>
                  <button
                    class="list-btn"
                    class:active={selectedId === hub.id}
                    onclick={() => {
                      if (viewMode !== 'symbols') switchView('symbols');
                      const found = graph.nodes.find((n) => n.id === hub.id);
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
        {:else}
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
                  <span class="meta">{node.file.split('/').pop()}</span>
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
          </defs>

          <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
            {#each visibleEdges as edge}
              {@const a = positions[edge.source]}
              {@const b = positions[edge.target]}
              {#if a && b}
                {@const inPath = pathEdgeSet.has(`${edge.source}->${edge.target}`)}
                {@const inCycle = cycleNodeSet.has(edge.source) && cycleNodeSet.has(edge.target)}
                {@const active = inPath || inCycle || selectedId === edge.source || selectedId === edge.target}
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={inPath ? '#22c55e' : inCycle ? '#f97316' : active ? '#38bdf8' : '#334155'}
                  stroke-width={inPath || inCycle ? 2.8 : active ? 2.2 : 1.2}
                  stroke-opacity={(selectedId || highlightedCycle) && !active ? 0.2 : 0.88}
                  marker-end={inPath ? 'url(#arrow-path)' : inCycle ? 'url(#arrow-cycle)' : active ? 'url(#arrow-active)' : 'url(#arrow)'}
                />
              {/if}
            {/each}

            {#each visibleNodes as node}
              {@const p = positions[node.id] || { x: 0, y: 0 }}
              {@const isSel = selectedId === node.id}
              {@const inCycle = cycleNodeSet.has(node.id)}
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
                  fill="#0f172a"
                  stroke={isSel ? '#f8fafc' : inCycle ? '#f97316' : color}
                  stroke-width={isSel || inCycle ? 3 : 2}
                />
                <text y="26" text-anchor="middle" class="node-label">
                  {node.name}
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
          {/if}
        </div>

        <div class="path-box">
          <h4>Path Finder</h4>
          <select bind:value={pathTargetId} class="full-sel">
            <option value="">Select target to trace path...</option>
            {#each activeData.nodes as n}
              {#if n.id !== selectedId}
                <option value={n.id}>{n.name} ({n.file.split('/').pop()})</option>
              {/if}
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
                <button class="chip in" onclick={() => selectNode(c)}>{c.name}</button>
              {:else}
                <span class="muted">None</span>
              {/each}
            </div>
          </div>
          <div>
            <h4>Calls ({selectedCallees.length})</h4>
            <div class="chips">
              {#each selectedCallees as c}
                <button class="chip out" onclick={() => selectNode(c)}>{c.name}</button>
              {:else}
                <span class="muted">None</span>
              {/each}
            </div>
          </div>
        </div>

        <div class="code-box">
          <h4>Source Preview</h4>
          {#if snippetLoading}
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
    gap: 0.75rem;
    padding: 0.6rem 1rem;
    background: #0f172a;
    border-bottom: 1px solid #1e293b;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }

  .logo {
    color: #38bdf8;
    font-size: 1.3rem;
  }

  .root-path {
    display: block;
    font-size: 0.72rem;
    color: #94a3b8;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stats {
    display: flex;
    gap: 0.5rem;
    font-size: 0.76rem;
  }

  .stats span {
    background: #1e293b;
    padding: 0.22rem 0.5rem;
    border-radius: 999px;
    color: #cbd5e1;
  }

  .stats .warn {
    background: #7c2d12;
    color: #fed7aa;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    flex-wrap: wrap;
  }

  .seg,
  .tabs {
    display: flex;
    background: #020617;
    border: 1px solid #334155;
    border-radius: 6px;
    overflow: hidden;
  }

  .tabs {
    margin-bottom: 0.65rem;
  }

  .seg button,
  .tabs button {
    border: none;
    border-radius: 0;
    background: transparent;
    padding: 0.35rem 0.55rem;
    font-size: 0.76rem;
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
    padding: 0.35rem 0.55rem;
    font-size: 0.8rem;
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
    gap: 0.3rem;
    font-size: 0.78rem;
    color: #cbd5e1;
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
    overflow-y: auto;
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
    grid-template-columns: 1fr 1fr;
    gap: 0.6rem;
    margin: 1rem 0;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
  }

  .chip {
    font-size: 0.75rem;
    padding: 0.2rem 0.45rem;
    border-radius: 4px;
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
</style>
