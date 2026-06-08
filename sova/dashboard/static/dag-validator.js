/* Client-side DAG validation -- cycle detection and input coverage check. */

(function() {
  'use strict';

  /**
   * Validate a DAG graph JSON structure.
   * @param {Object} graph - { nodes: [...], edges: [...] }
   * @returns {string[]} Array of error strings (empty = valid)
   */
  function validateDAG(graph) {
    const errors = [];
    if (!graph || typeof graph !== 'object') {
      errors.push('Invalid graph: expected an object');
      return errors;
    }
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];

    if (!nodes.length) {
      errors.push('DAG has no nodes');
      return errors;
    }

    const nodeIds = new Set(nodes.map(n => n.id));

    // Validate edges reference existing nodes
    for (const edge of edges) {
      if (!nodeIds.has(edge.source)) {
        errors.push(`Edge references unknown source: ${edge.source}`);
      }
      if (!nodeIds.has(edge.target)) {
        errors.push(`Edge references unknown target: ${edge.target}`);
      }
    }

    // Validate nodes have commands
    for (const node of nodes) {
      if (!node.command) {
        errors.push(`Node ${node.id} has no command`);
      }
    }

    // Cycle detection (Kahn's algorithm)
    const inDegree = {};
    const adj = {};
    for (const id of nodeIds) {
      inDegree[id] = 0;
      adj[id] = [];
    }
    for (const edge of edges) {
      if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) {
        adj[edge.source].push(edge.target);
        inDegree[edge.target]++;
      }
    }

    const queue = [];
    for (const [id, deg] of Object.entries(inDegree)) {
      if (deg === 0) queue.push(id);
    }

    let sorted = 0;
    while (queue.length) {
      const nid = queue.shift();
      sorted++;
      for (const neighbor of adj[nid]) {
        inDegree[neighbor]--;
        if (inDegree[neighbor] === 0) queue.push(neighbor);
      }
    }

    if (sorted !== nodeIds.size) {
      errors.push('DAG contains a cycle');
    }

    return errors;
  }

  // Expose globally
  window.validateDAGClient = validateDAG;
})();
