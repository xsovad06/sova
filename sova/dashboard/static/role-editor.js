/* Role editor -- cytoscape.js DAG visualization and manipulation. */

(function() {
  'use strict';

  const root = document.getElementById('editor-root');
  const roleName = encodeURIComponent(root.dataset.roleName);
  const prefix = (document.querySelector('[data-tooltip="Roles"]')?.closest('a')?.href || '').replace(/\/roles$/, '') || '';
  const apiBase = prefix ? `${prefix}/api` : '/api';

  const _css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  // Reuse global escapeHtml or provide fallback
  const esc = window.escapeHtml || function(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  };

  let cy = null;
  let roleData = null;
  let isBuiltin = false;
  let commands = [];
  let nodeIdCounter = 100;

  const TASK_STATES = ['backlog', 'triaged', 'researched', 'in_progress', 'in_review', 'done', 'needs_spec', 'human_only'];

  // -- Init -------------------------------------------------------------------

  async function init() {
    await Promise.all([loadRole(), loadCommands()]);
    initCytoscape();
    renderCommandPalette();
    if (!isBuiltin) {
      document.getElementById('btn-save').classList.remove('hidden');
      initStateConfig();
    }
  }

  function initStateConfig() {
    const settingsEl = document.getElementById('role-settings');
    settingsEl.classList.remove('hidden');

    const checkboxes = document.getElementById('input-states-checkboxes');
    const currentInputs = new Set(roleData.input_states || []);
    checkboxes.innerHTML = TASK_STATES.map(s => {
      const checked = currentInputs.has(s) ? 'checked' : '';
      return `<label class="flex items-center gap-1 text-xs text-gray-300 cursor-pointer select-none">
        <input type="checkbox" value="${esc(s)}" ${checked} class="js-input-state rounded border-gray-600 bg-surface text-accent focus:ring-accent/50">
        <span>${esc(s)}</span>
      </label>`;
    }).join('');

    const select = document.getElementById('output-state-select');
    select.innerHTML = '<option value="">None (no transition)</option>' +
      TASK_STATES.map(s => {
        const selected = (roleData.output_state === s) ? 'selected' : '';
        return `<option value="${esc(s)}" ${selected}>${esc(s)}</option>`;
      }).join('');
  }

  async function loadRole() {
    const resp = await fetch(`${apiBase}/roles/${roleName}`);
    if (!resp.ok) {
      document.getElementById('role-title').textContent = 'Failed to load role';
      throw new Error(`Role load failed: ${resp.status}`);
    }
    roleData = await resp.json();
    isBuiltin = !!roleData.is_builtin;
    document.getElementById('role-title').textContent = roleData.name || decodeURIComponent(roleName);
    if (isBuiltin) {
      document.getElementById('builtin-badge').classList.remove('hidden');
      document.getElementById('command-palette').classList.add('opacity-50', 'pointer-events-none');
    }
  }

  async function loadCommands() {
    try {
      const resp = await fetch(`${apiBase}/roles/commands`);
      const data = await resp.json();
      commands = data.commands || [];
    } catch (e) {
      commands = [];
    }
  }

  // -- Cytoscape setup --------------------------------------------------------

  function initCytoscape() {
    const graph = roleData.graph_json || { nodes: [], edges: [] };

    // Advance nodeIdCounter past any existing numeric IDs to prevent collisions
    for (const item of [...(graph.nodes || []), ...(graph.edges || [])]) {
      const m = String(item.id).match(/^[ne](\d+)$/);
      if (m) nodeIdCounter = Math.max(nodeIdCounter, parseInt(m[1], 10));
    }

    const elements = [];

    for (const node of (graph.nodes || [])) {
      elements.push({
        data: {
          id: node.id,
          label: node.label || node.command || node.id,
          command: node.command || '',
          params: node.params || {},
        },
        position: node.position || undefined,
      });
    }

    for (const edge of (graph.edges || [])) {
      elements.push({
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          condition: edge.condition || '',
          label: edge.condition || '',
        },
      });
    }

    cy = cytoscape({
      container: document.getElementById('cy'),
      elements: elements,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': _css('--ctp-blue'),
            'label': 'data(label)',
            'color': _css('--ctp-text'),
            'text-valign': 'bottom',
            'text-margin-y': 8,
            'font-size': '11px',
            'width': 40,
            'height': 40,
            'border-width': 2,
            'border-color': _css('--ctp-surface2'),
          }
        },
        {
          selector: 'node:selected',
          style: {
            'border-color': _css('--ctp-mauve'),
            'border-width': 3,
          }
        },
        {
          selector: 'edge',
          style: {
            'width': 2,
            'line-color': _css('--ctp-surface2'),
            'target-arrow-color': _css('--ctp-surface2'),
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'label': 'data(label)',
            'font-size': '9px',
            'color': _css('--ctp-subtext0'),
            'text-rotation': 'autorotate',
          }
        },
        {
          selector: 'edge:selected',
          style: {
            'line-color': _css('--ctp-mauve'),
            'target-arrow-color': _css('--ctp-mauve'),
          }
        },
      ],
      layout: graph.nodes?.some(n => n.position) ? { name: 'preset' } : {
        name: 'dagre',
        rankDir: 'TB',
        nodeSep: 60,
        rankSep: 80,
      },
      userZoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
    });

    cy.on('tap', 'node', function(evt) { showNodeProps(evt.target); });
    cy.on('tap', 'edge', function(evt) { showEdgeProps(evt.target); });
    cy.on('tap', function(evt) {
      if (evt.target === cy) {
        document.getElementById('props-content').innerHTML = '<p class="text-xs text-gray-500">Select a node or edge</p>';
      }
    });

    // Auto-layout if no positions
    if (!graph.nodes?.some(n => n.position)) {
      cy.layout({ name: 'dagre', rankDir: 'TB', nodeSep: 60, rankSep: 80 }).run();
    }
  }

  // -- Command palette --------------------------------------------------------

  function renderCommandPalette() {
    const list = document.getElementById('command-list');
    if (!commands.length) {
      list.innerHTML = '<p class="text-xs text-gray-500 p-2">No commands found</p>';
      return;
    }

    const byCategory = {};
    for (const cmd of commands) {
      const cat = cmd.category || 'other';
      if (!byCategory[cat]) byCategory[cat] = [];
      byCategory[cat].push(cmd);
    }

    let html = '';
    for (const [cat, cmds] of Object.entries(byCategory).sort()) {
      html += `<p class="text-[10px] text-gray-500 uppercase tracking-wide px-2 pt-2 pb-1">${esc(cat)}</p>`;
      for (const cmd of cmds) {
        html += `<button class="js-add-node w-full text-left text-xs px-2 py-1.5 rounded hover:bg-sidebar-hover text-gray-300 transition-colors"
                         data-command="${esc(cmd.name)}"
                         title="${esc(cmd.description || '')}">${esc(cmd.name)}</button>`;
      }
    }
    list.innerHTML = html;

    // Attach event listeners for command palette buttons
    list.querySelectorAll('.js-add-node').forEach(btn => {
      btn.addEventListener('click', () => window._addNode(btn.dataset.command));
    });
  }

  // -- Node/edge manipulation -------------------------------------------------

  window._addNode = function(commandName) {
    if (isBuiltin) return;
    let id = `n${++nodeIdCounter}`;
    while (cy.getElementById(id).length) {
      id = `n${++nodeIdCounter}`;
    }
    const center = cy.extent();
    cy.add({
      data: { id, label: commandName, command: commandName, params: {} },
      position: { x: (center.x1 + center.x2) / 2, y: (center.y1 + center.y2) / 2 },
    });
  };

  function showNodeProps(node) {
    const data = node.data();
    const editable = !isBuiltin;
    const cmd = commands.find(c => c.name === data.command);
    const inputs = cmd?.inputs || [];
    const outputs = cmd?.outputs || [];

    let html = `
      <div class="space-y-3">
        <div>
          <label class="text-[10px] text-gray-500 block mb-1">ID</label>
          <p class="text-xs text-gray-400">${esc(data.id)}</p>
        </div>
        <div>
          <label class="text-[10px] text-gray-500 block mb-1">Command</label>
          ${cmd ? `<a href="${prefix}/commands#${encodeURIComponent(data.command)}" class="text-xs text-accent hover:underline">${esc(data.command)}</a>` : `<p class="text-xs text-gray-300">${esc(data.command || '(none)')}</p>`}
        </div>
        <div>
          <label class="text-[10px] text-gray-500 block mb-1">Label</label>
          ${editable
            ? `<input type="text" value="${esc(data.label)}" data-node-id="${esc(data.id)}" class="js-node-label w-full bg-surface text-xs text-gray-300 border border-gray-700/50 rounded px-2 py-1">`
            : `<p class="text-xs text-gray-300">${esc(data.label)}</p>`}
        </div>`;

    if (inputs.length) {
      html += `<div><label class="text-[10px] text-gray-500 block mb-1">Inputs</label>
        <div class="flex flex-wrap gap-1">${inputs.map(i => `<span class="text-[10px] px-1.5 py-0.5 rounded bg-accent-purple/20 text-accent-purple">${esc(i)}</span>`).join('')}</div></div>`;
    }
    if (outputs.length) {
      html += `<div><label class="text-[10px] text-gray-500 block mb-1">Outputs</label>
        <div class="flex flex-wrap gap-1">${outputs.map(o => `<span class="text-[10px] px-1.5 py-0.5 rounded bg-accent-green/20 text-accent-green">${esc(o)}</span>`).join('')}</div></div>`;
    }

    if (editable) {
      html += `<div class="pt-2 border-t border-gray-700/30 flex gap-2">
        <button data-node-id="${esc(data.id)}" class="js-connect-from text-[10px] px-2 py-1 rounded bg-accent/20 text-accent hover:bg-accent/30 transition-colors">Add edge from</button>
        <button data-node-id="${esc(data.id)}" class="js-delete-node text-[10px] px-2 py-1 rounded bg-accent-red/20 text-accent-red hover:bg-accent-red/30 transition-colors">Delete</button>
      </div>`;
    }

    html += '</div>';
    const propsEl = document.getElementById('props-content');
    propsEl.innerHTML = html;

    // Attach event listeners for node properties
    const labelInput = propsEl.querySelector('.js-node-label');
    if (labelInput) {
      labelInput.addEventListener('change', function() {
        window._updateNodeLabel(this.dataset.nodeId, this.value);
      });
    }
    const connectBtn = propsEl.querySelector('.js-connect-from');
    if (connectBtn) {
      connectBtn.addEventListener('click', function() {
        window._connectFrom(this.dataset.nodeId);
      });
    }
    const deleteBtn = propsEl.querySelector('.js-delete-node');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', function() {
        window._deleteNode(this.dataset.nodeId);
      });
    }
  }

  function showEdgeProps(edge) {
    const data = edge.data();
    const editable = !isBuiltin;

    let html = `
      <div class="space-y-3">
        <div>
          <label class="text-[10px] text-gray-500 block mb-1">Source</label>
          <p class="text-xs text-gray-400">${esc(data.source)}</p>
        </div>
        <div>
          <label class="text-[10px] text-gray-500 block mb-1">Target</label>
          <p class="text-xs text-gray-400">${esc(data.target)}</p>
        </div>
        <div>
          <label class="text-[10px] text-gray-500 block mb-1">Condition</label>
          ${editable
            ? `<input type="text" value="${esc(data.condition || '')}" placeholder="key == value" data-edge-id="${esc(data.id)}" class="js-edge-condition w-full bg-surface text-xs text-gray-300 border border-gray-700/50 rounded px-2 py-1">`
            : `<p class="text-xs text-gray-300">${esc(data.condition || '(unconditional)')}</p>`}
        </div>`;

    if (editable) {
      html += `<div class="pt-2 border-t border-gray-700/30">
        <button data-edge-id="${esc(data.id)}" class="js-delete-edge text-[10px] px-2 py-1 rounded bg-accent-red/20 text-accent-red hover:bg-accent-red/30 transition-colors">Delete edge</button>
      </div>`;
    }

    html += '</div>';
    const propsEl = document.getElementById('props-content');
    propsEl.innerHTML = html;

    // Attach event listeners for edge properties
    const condInput = propsEl.querySelector('.js-edge-condition');
    if (condInput) {
      condInput.addEventListener('change', function() {
        window._updateEdgeCondition(this.dataset.edgeId, this.value);
      });
    }
    const delBtn = propsEl.querySelector('.js-delete-edge');
    if (delBtn) {
      delBtn.addEventListener('click', function() {
        window._deleteEdge(this.dataset.edgeId);
      });
    }
  }

  let pendingEdgeSource = null;

  window._connectFrom = function(sourceId) {
    pendingEdgeSource = sourceId;
    if (window.showToast) window.showToast('Click a target node to create an edge', 'info');
    cy.one('tap', 'node', function(evt) {
      const targetId = evt.target.data('id');
      if (targetId !== pendingEdgeSource) {
        let edgeId = `e${++nodeIdCounter}`;
        while (cy.getElementById(edgeId).length) {
          edgeId = `e${++nodeIdCounter}`;
        }
        cy.add({ data: { id: edgeId, source: pendingEdgeSource, target: targetId, condition: '', label: '' } });
      }
      pendingEdgeSource = null;
    });
  };

  window._updateNodeLabel = function(nodeId, label) {
    cy.getElementById(nodeId).data('label', label);
  };

  window._updateEdgeCondition = function(edgeId, condition) {
    const edge = cy.getElementById(edgeId);
    edge.data('condition', condition);
    edge.data('label', condition);
  };

  window._deleteNode = function(nodeId) {
    if (isBuiltin) return;
    cy.getElementById(nodeId).remove();
  };

  window._deleteEdge = function(edgeId) {
    if (isBuiltin) return;
    cy.getElementById(edgeId).remove();
  };

  // -- Save / Validate --------------------------------------------------------

  function buildGraphJSON() {
    const nodes = cy.nodes().map(n => ({
      id: n.data('id'),
      command: n.data('command') || '',
      label: n.data('label') || '',
      position: n.position(),
      params: n.data('params') || {},
    }));

    const edges = cy.edges().map(e => ({
      id: e.data('id'),
      source: e.data('source'),
      target: e.data('target'),
      condition: e.data('condition') || null,
    }));

    return { nodes, edges };
  }

  window.saveRole = async function() {
    if (isBuiltin) return;
    const graph = buildGraphJSON();
    const inputStates = Array.from(document.querySelectorAll('.js-input-state:checked')).map(cb => cb.value);
    const outputState = document.getElementById('output-state-select').value;

    try {
      const resp = await fetch(`${apiBase}/roles/${roleName}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ graph_json: graph, input_states: inputStates, output_state: outputState }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({ detail: 'Save failed' }));
        const detail = data.detail;
        const errors = (typeof detail === 'object' && detail.validation_errors) || [detail || 'Save failed'];
        showValidation(Array.isArray(errors) ? errors : [String(errors)], true);
        return;
      }
      if (window.showToast) window.showToast('Role saved', 'success');
    } catch (e) {
      if (window.showToast) window.showToast('Failed to save', 'error');
    }
  };

  window.validateDAG = async function() {
    const graph = buildGraphJSON();
    try {
      const resp = await fetch(`${apiBase}/roles/${roleName}/validate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ graph_json: graph }),
      });
      if (!resp.ok) {
        showValidation([`Validation request failed (${resp.status})`], true);
        return;
      }
      const data = await resp.json();
      showValidation(data.errors || [], !data.valid);
    } catch (e) {
      showValidation(['Validation request failed'], true);
    }
  };

  function showValidation(errors, isError) {
    const bar = document.getElementById('validation-bar');
    bar.classList.remove('hidden');
    if (isError && errors.length) {
      bar.className = 'mt-3 p-3 rounded-lg border text-sm border-accent-red/30 bg-accent-red/10 text-accent-red';
      bar.innerHTML = errors.map(e => `<p>${esc(e)}</p>`).join('');
    } else {
      bar.className = 'mt-3 p-3 rounded-lg border text-sm border-accent-green/30 bg-accent-green/10 text-accent-green';
      bar.innerHTML = '<p>DAG is valid</p>';
      setTimeout(() => bar.classList.add('hidden'), 3000);
    }
  }

  // -- Bootstrap --------------------------------------------------------------

  init();
})();
