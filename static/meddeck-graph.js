/**
 * MEDDECK Interactive Network Graph Engine
 * High-performance force-directed canvas graph visualizer for HPO, Genes, Communities and Patients.
 */
class MeddeckGraph {
  constructor(containerId, options = {}) {
    this.container = document.getElementById(containerId);
    if (!this.container) return;

    this.options = Object.assign(
      {
        interactive: true,
        showControls: true,
        height: 520,
        enablePhysics: true,
        nodeRadius: 22,
      },
      options
    );

    this.nodes = [];
    this.links = [];
    this.nodeMap = new Map();
    this.selectedNode = null;
    this.hoveredNode = null;

    this.transform = { x: 0, y: 0, k: 1 };
    this.isDragging = false;
    this.draggedNode = null;
    this.dragStartPos = { x: 0, y: 0 };
    this.animFrame = null;
    this.physicsRunning = true;
    this.activeFilters = {
      user: true,
      gene: true,
      phenotype: true,
      community: true,
      disease: true,
    };

    this.colors = {
      gene: { fill: "#ede9fe", stroke: "#7c3aed", text: "#5b21b6", badge: "Gene", icon: "🧬" },
      user: { fill: "#d1fae5", stroke: "#059669", text: "#065f46", badge: "Participant", icon: "👤" },
      phenotype: { fill: "#fef3c7", stroke: "#d97706", text: "#92400e", badge: "HPO Phenotype", icon: "🏷️" },
      community: { fill: "#e0f2fe", stroke: "#0284c7", text: "#075985", badge: "Community", icon: "🌐" },
      disease: { fill: "#fee2e2", stroke: "#dc2626", text: "#991b1b", badge: "Disease", icon: "🩺" },
      default: { fill: "#f3f4f6", stroke: "#6b7280", text: "#374151", badge: "Item", icon: "•" },
    };

    this.initDOM();
    this.bindEvents();
  }

  initDOM() {
    this.container.innerHTML = "";
    this.container.classList.add("meddeck-graph-wrapper");

    // Controls bar
    if (this.options.showControls) {
      const controls = document.createElement("div");
      controls.className = "graph-controls";
      controls.innerHTML = `
        <div class="graph-search-group">
          <span class="search-icon">🔍</span>
          <input type="text" class="graph-search-input" placeholder="Search genes, phenotypes, patients..." aria-label="Search the graph">
        </div>
        <div class="graph-filters">
          <label class="filter-chip filter-gene active"><input type="checkbox" data-type="gene" checked> <span>🧬 Genes</span></label>
          <label class="filter-chip filter-user active"><input type="checkbox" data-type="user" checked> <span>👤 Patients</span></label>
          <label class="filter-chip filter-phenotype active"><input type="checkbox" data-type="phenotype" checked> <span>🏷️ Phenotypes</span></label>
          <label class="filter-chip filter-community active"><input type="checkbox" data-type="community" checked> <span>🌐 Communities</span></label>
        </div>
        <div class="graph-actions">
          <button type="button" class="btn-graph-action" data-action="zoom-in" title="Zoom in (Zoom +)">＋</button>
          <button type="button" class="btn-graph-action" data-action="zoom-out" title="Zoom out (Zoom -)">－</button>
          <button type="button" class="btn-graph-action" data-action="reset" title="Reset view">⟲</button>
          <button type="button" class="btn-graph-action" data-action="physics" title="Pause/Resume motion">⏸</button>
        </div>
      `;
      this.container.appendChild(controls);
    }

    // Canvas container
    this.viewport = document.createElement("div");
    this.viewport.className = "graph-viewport";
    this.viewport.style.height = `${this.options.height}px`;

    this.canvas = document.createElement("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.viewport.appendChild(this.canvas);

    // Detail panel
    this.detailPanel = document.createElement("div");
    this.detailPanel.className = "graph-detail-panel";
    this.detailPanel.hidden = true;
    this.viewport.appendChild(this.detailPanel);

    this.container.appendChild(this.viewport);

    this.resize();
    window.addEventListener("resize", () => this.resize());
  }

  resize() {
    if (!this.viewport) return;
    const rect = this.viewport.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.width = rect.width || 800;
    this.height = rect.height || this.options.height;

    this.canvas.width = this.width * dpr;
    this.canvas.height = this.height * dpr;
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;

    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.scale(dpr, dpr);
    this.render();
  }

  setData(data) {
    if (!data) return;
    const rawNodes = data.nodes || [];
    const rawLinks = data.links || [];

    this.nodeMap.clear();
    const cx = this.width / 2;
    const cy = this.height / 2;

    this.nodes = rawNodes.map((n, i) => {
      const angle = (i / Math.max(1, rawNodes.length)) * 2 * Math.PI;
      const dist = 120 + (i % 3) * 60;
      const node = {
        id: String(n.id),
        name: n.name || n.id,
        type: n.type || "default",
        data: n,
        x: n.x !== undefined ? n.x : cx + Math.cos(angle) * dist + (Math.random() - 0.5) * 40,
        y: n.y !== undefined ? n.y : cy + Math.sin(angle) * dist + (Math.random() - 0.5) * 40,
        vx: 0,
        vy: 0,
        radius: n.radius || (n.type === "gene" ? 26 : n.type === "community" ? 28 : 22),
      };
      this.nodeMap.set(node.id, node);
      return node;
    });

    this.links = rawLinks
      .map((link) => {
        const source = this.nodeMap.get(String(link.source));
        const target = this.nodeMap.get(String(link.target));
        if (source && target) {
          return {
            source,
            target,
            type: link.type || "default",
            label: link.label || "",
          };
        }
        return null;
      })
      .filter(Boolean);

    this.resetView();
    this.startPhysics();
  }

  resetView() {
    this.transform = { x: 0, y: 0, k: 1 };
    this.selectedNode = null;
    this.hoveredNode = null;
    if (this.detailPanel) this.detailPanel.hidden = true;
    this.render();
  }

  startPhysics() {
    if (this.animFrame) cancelAnimationFrame(this.animFrame);
    let ticks = 0;
    const loop = () => {
      if (this.physicsRunning && ticks < 400) {
        this.stepPhysics();
        ticks++;
      }
      this.render();
      this.animFrame = requestAnimationFrame(loop);
    };
    this.animFrame = requestAnimationFrame(loop);
  }

  stepPhysics() {
    const visibleNodes = this.nodes.filter((n) => this.activeFilters[n.type] !== false);
    const visibleSet = new Set(visibleNodes.map((n) => n.id));
    const visibleLinks = this.links.filter(
      (l) => visibleSet.has(l.source.id) && visibleSet.has(l.target.id)
    );

    const cx = this.width / 2;
    const cy = this.height / 2;

    // Node-Node Repulsion
    for (let i = 0; i < visibleNodes.length; i++) {
      const n1 = visibleNodes[i];
      for (let j = i + 1; j < visibleNodes.length; j++) {
        const n2 = visibleNodes[j];
        const dx = n2.x - n1.x;
        const dy = n2.y - n1.y;
        const distSq = dx * dx + dy * dy || 1;
        const dist = Math.sqrt(distSq);
        const minDist = n1.radius + n2.radius + 35;
        const force = (minDist * minDist) / (distSq * 1.8);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        if (n1 !== this.draggedNode) {
          n1.vx -= fx;
          n1.vy -= fy;
        }
        if (n2 !== this.draggedNode) {
          n2.vx += fx;
          n2.vy += fy;
        }
      }
    }

    // Link Spring Attraction
    for (const link of visibleLinks) {
      const dx = link.target.x - link.source.x;
      const dy = link.target.y - link.source.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const targetDist = 110;
      const force = (dist - targetDist) * 0.04;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      if (link.source !== this.draggedNode) {
        link.source.vx += fx;
        link.source.vy += fy;
      }
      if (link.target !== this.draggedNode) {
        link.target.vx -= fx;
        link.target.vy -= fy;
      }
    }

    // Centering & Velocity damping
    for (const node of visibleNodes) {
      if (node === this.draggedNode) continue;
      node.vx += (cx - node.x) * 0.003;
      node.vy += (cy - node.y) * 0.003;
      node.vx *= 0.86;
      node.vy *= 0.86;
      node.x += Math.max(-15, Math.min(15, node.vx));
      node.y += Math.max(-15, Math.min(15, node.vy));
    }
  }

  render() {
    if (!this.ctx) return;
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.width, this.height);

    // Save and transform
    ctx.save();
    ctx.translate(this.transform.x, this.transform.y);
    ctx.scale(this.transform.k, this.transform.k);

    const visibleNodes = this.nodes.filter((n) => this.activeFilters[n.type] !== false);
    const visibleSet = new Set(visibleNodes.map((n) => n.id));
    const visibleLinks = this.links.filter(
      (l) => visibleSet.has(l.source.id) && visibleSet.has(l.target.id)
    );

    // Draw Links
    for (const link of visibleLinks) {
      const isHighlighted =
        this.hoveredNode &&
        (link.source.id === this.hoveredNode.id || link.target.id === this.hoveredNode.id);
      const isSelected =
        this.selectedNode &&
        (link.source.id === this.selectedNode.id || link.target.id === this.selectedNode.id);

      ctx.beginPath();
      ctx.moveTo(link.source.x, link.source.y);
      ctx.lineTo(link.target.x, link.target.y);
      ctx.strokeStyle = isSelected
        ? "#27564f"
        : isHighlighted
        ? "#53877d"
        : "#dce7e2";
      ctx.lineWidth = isSelected ? 3 : isHighlighted ? 2.5 : 1.5;
      if (!isSelected && !isHighlighted && this.hoveredNode) {
        ctx.strokeStyle = "rgba(220, 231, 226, 0.4)";
      }
      ctx.stroke();

      // Optional edge label
      if (link.label && (isSelected || isHighlighted)) {
        const mx = (link.source.x + link.target.x) / 2;
        const my = (link.source.y + link.target.y) / 2;
        ctx.font = "10px sans-serif";
        ctx.fillStyle = "#627774";
        ctx.textAlign = "center";
        ctx.fillText(link.label, mx, my - 4);
      }
    }

    // Draw Nodes
    for (const node of visibleNodes) {
      const style = this.colors[node.type] || this.colors.default;
      const isHovered = this.hoveredNode && this.hoveredNode.id === node.id;
      const isSelected = this.selectedNode && this.selectedNode.id === node.id;
      const isNeighbor =
        this.hoveredNode &&
        visibleLinks.some(
          (l) =>
            (l.source.id === this.hoveredNode.id && l.target.id === node.id) ||
            (l.target.id === this.hoveredNode.id && l.source.id === node.id)
        );

      let opacity = 1;
      if (this.hoveredNode && !isHovered && !isNeighbor && !isSelected) {
        opacity = 0.35;
      }

      ctx.save();
      ctx.globalAlpha = opacity;

      // Outer glow for selected/hovered
      if (isSelected || isHovered) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.radius + 6, 0, 2 * Math.PI);
        ctx.fillStyle = style.stroke + "22";
        ctx.fill();
      }

      // Circle Fill
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.radius, 0, 2 * Math.PI);
      ctx.fillStyle = style.fill;
      ctx.fill();
      ctx.strokeStyle = isSelected ? "#183634" : style.stroke;
      ctx.lineWidth = isSelected ? 3 : 2;
      ctx.stroke();

      // Icon or short label inside circle
      ctx.font = `${Math.round(node.radius * 0.9)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(style.icon, node.x, node.y + 1);

      // Node Name Label below circle
      ctx.font = `600 ${isSelected || isHovered ? "12px" : "11px"} Manrope, "DM Sans", sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const labelText = node.name.length > 24 ? node.name.slice(0, 22) + "…" : node.name;

      // Label background badge for clarity
      const textWidth = ctx.measureText(labelText).width;
      ctx.fillStyle = "rgba(255, 255, 255, 0.92)";
      ctx.fillRect(node.x - textWidth / 2 - 4, node.y + node.radius + 3, textWidth + 8, 16);
      ctx.strokeStyle = style.stroke + "44";
      ctx.lineWidth = 1;
      ctx.strokeRect(node.x - textWidth / 2 - 4, node.y + node.radius + 3, textWidth + 8, 16);

      ctx.fillStyle = isSelected ? "#183634" : style.text;
      ctx.fillText(labelText, node.x, node.y + node.radius + 5);

      ctx.restore();
    }

    ctx.restore();
  }

  screenToWorld(sx, sy) {
    const rect = this.canvas.getBoundingClientRect();
    const x = (sx - rect.left - this.transform.x) / this.transform.k;
    const y = (sy - rect.top - this.transform.y) / this.transform.k;
    return { x, y };
  }

  getNodeAt(sx, sy) {
    const { x, y } = this.screenToWorld(sx, sy);
    const visibleNodes = this.nodes.filter((n) => this.activeFilters[n.type] !== false);
    for (let i = visibleNodes.length - 1; i >= 0; i--) {
      const node = visibleNodes[i];
      const dx = node.x - x;
      const dy = node.y - y;
      if (dx * dx + dy * dy <= (node.radius + 8) * (node.radius + 8)) {
        return node;
      }
    }
    return null;
  }

  bindEvents() {
    this.canvas.addEventListener("mousedown", (e) => {
      const node = this.getNodeAt(e.clientX, e.clientY);
      this.isDragging = true;
      this.dragStartPos = { x: e.clientX, y: e.clientY };

      if (node) {
        this.draggedNode = node;
        this.selectedNode = node;
        this.showDetail(node);
      } else {
        this.draggedNode = null;
      }
    });

    window.addEventListener("mousemove", (e) => {
      if (!this.viewport.contains(e.target) && !this.isDragging) return;

      if (this.isDragging) {
        const dx = e.clientX - this.dragStartPos.x;
        const dy = e.clientY - this.dragStartPos.y;
        this.dragStartPos = { x: e.clientX, y: e.clientY };

        if (this.draggedNode) {
          this.draggedNode.x += dx / this.transform.k;
          this.draggedNode.y += dy / this.transform.k;
          this.draggedNode.vx = 0;
          this.draggedNode.vy = 0;
        } else {
          this.transform.x += dx;
          this.transform.y += dy;
        }
        this.render();
      } else {
        const node = this.getNodeAt(e.clientX, e.clientY);
        if (node !== this.hoveredNode) {
          this.hoveredNode = node;
          this.canvas.style.cursor = node ? "pointer" : "grab";
          this.render();
        }
      }
    });

    window.addEventListener("mouseup", () => {
      this.isDragging = false;
      this.draggedNode = null;
    });

    // Zoom on wheel
    this.canvas.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.1 : 0.9;
        const rect = this.canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        const newK = Math.max(0.3, Math.min(3.5, this.transform.k * factor));
        this.transform.x = mouseX - (mouseX - this.transform.x) * (newK / this.transform.k);
        this.transform.y = mouseY - (mouseY - this.transform.y) * (newK / this.transform.k);
        this.transform.k = newK;
        this.render();
      },
      { passive: false }
    );

    // Filter and actions bindings
    if (this.options.showControls) {
      const filterInputs = this.container.querySelectorAll(".graph-filters input");
      filterInputs.forEach((input) => {
        input.addEventListener("change", (e) => {
          const type = e.target.getAttribute("data-type");
          this.activeFilters[type] = e.target.checked;
          e.target.closest(".filter-chip").classList.toggle("active", e.target.checked);
          this.render();
        });
      });

      const searchInput = this.container.querySelector(".graph-search-input");
      if (searchInput) {
        searchInput.addEventListener("input", (e) => {
          const query = e.target.value.trim().toLowerCase();
          if (!query) {
            this.hoveredNode = null;
            this.render();
            return;
          }
          const matched = this.nodes.find(
            (n) =>
              n.name.toLowerCase().includes(query) ||
              (n.data && n.data.hpo_id && n.data.hpo_id.toLowerCase().includes(query))
          );
          if (matched) {
            this.focusNode(matched);
          }
        });
      }

      this.container.querySelectorAll(".btn-graph-action").forEach((btn) => {
        btn.addEventListener("click", () => {
          const action = btn.getAttribute("data-action");
          if (action === "zoom-in") {
            this.zoomBy(1.2);
          } else if (action === "zoom-out") {
            this.zoomBy(0.8);
          } else if (action === "reset") {
            this.resetView();
          } else if (action === "physics") {
            this.physicsRunning = !this.physicsRunning;
            btn.textContent = this.physicsRunning ? "⏸" : "▶";
            btn.title = this.physicsRunning ? "Pause motion" : "Resume motion";
          }
        });
      });
    }
  }

  zoomBy(factor) {
    const cx = this.width / 2;
    const cy = this.height / 2;
    const newK = Math.max(0.3, Math.min(3.5, this.transform.k * factor));
    this.transform.x = cx - (cx - this.transform.x) * (newK / this.transform.k);
    this.transform.y = cy - (cy - this.transform.y) * (newK / this.transform.k);
    this.transform.k = newK;
    this.render();
  }

  focusNode(node) {
    this.selectedNode = node;
    this.hoveredNode = node;
    this.transform.k = 1.3;
    this.transform.x = this.width / 2 - node.x * this.transform.k;
    this.transform.y = this.height / 2 - node.y * this.transform.k;
    this.showDetail(node);
    this.render();
  }

  showDetail(node) {
    if (!this.detailPanel) return;
    const style = this.colors[node.type] || this.colors.default;
    const connectedLinks = this.links.filter(
      (l) => l.source.id === node.id || l.target.id === node.id
    );
    const neighbors = connectedLinks.map((l) => (l.source.id === node.id ? l.target : l.source));

    let actionBtnHtml = "";
    if (node.type === "gene") {
      actionBtnHtml = `
        <form method="post" action="/join-candidate-gene" class="detail-action-form">
          <input type="hidden" name="csrf_token" value="${document.querySelector('input[name="csrf_token"]')?.value || ""}">
          <input type="hidden" name="gene" value="${node.name}">
          <button type="submit" class="button secondary small-btn">Join ${node.name} community</button>
        </form>
      `;
    }

    this.detailPanel.innerHTML = `
      <div class="detail-header">
        <span class="detail-tag" style="background:${style.fill};color:${style.text};border:1px solid ${style.stroke}">
          ${style.icon} ${style.badge}
        </span>
        <button type="button" class="btn-close-detail" aria-label="Close details">✕</button>
      </div>
      <h3 class="detail-title">${node.name}</h3>
      ${node.data?.hpo_id ? `<p class="detail-code"><code>${node.data.hpo_id}</code></p>` : ""}
      <p class="detail-connections-count"><strong>${neighbors.length}</strong> direct connection(s)</p>
      <div class="detail-neighbors-list">
        ${neighbors
          .slice(0, 8)
          .map((n) => {
            const nStyle = this.colors[n.type] || this.colors.default;
            return `<span class="neighbor-chip" data-id="${n.id}" style="border-left: 3px solid ${nStyle.stroke}">${nStyle.icon} ${n.name}</span>`;
          })
          .join("")}
        ${neighbors.length > 8 ? `<span class="muted small-note">+${neighbors.length - 8} more...</span>` : ""}
      </div>
      ${actionBtnHtml}
    `;

    this.detailPanel.hidden = false;

    this.detailPanel.querySelector(".btn-close-detail")?.addEventListener("click", () => {
      this.detailPanel.hidden = true;
      this.selectedNode = null;
      this.render();
    });

    this.detailPanel.querySelectorAll(".neighbor-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const targetId = chip.getAttribute("data-id");
        const targetNode = this.nodeMap.get(targetId);
        if (targetNode) this.focusNode(targetNode);
      });
    });
  }
}

window.MeddeckGraph = MeddeckGraph;
