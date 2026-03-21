const container = document.getElementById("viz-container");
const width = container.clientWidth;
const height = container.clientHeight;

const colors = {
  "Normal Whale": "#0f766e",
  "Suspicious (New Account(10 Trades))": "#d77a61",
  "Suspicious (>80.0% Concentration)": "#ea580c",
  "Critical Risk (Both Flags)": "#be123c",
};

const svg = d3
  .select("#viz-container")
  .append("svg")
  .attr("width", width)
  .attr("height", height);

const zoomGroup = svg.append("g");
const zoom = d3
  .zoom()
  .scaleExtent([0.3, 5])
  .on("zoom", (event) => zoomGroup.attr("transform", event.transform));
svg.call(zoom);
svg.call(zoom.transform, d3.zoomIdentity.translate(width / 2, height / 2));

const maxRadius = Math.min(width, height) / 2 - 80;

zoomGroup
  .append("circle")
  .attr("cx", 0)
  .attr("cy", 0)
  .attr("r", maxRadius * 0.3)
  .attr("fill", "rgba(190, 18, 60, 0.04)")
  .attr("stroke", "rgba(190, 18, 60, 0.2)")
  .attr("stroke-dasharray", "4,4");
zoomGroup
  .append("text")
  .attr("class", "annotation-text")
  .attr("x", 0)
  .attr("y", -(maxRadius * 0.3) - 10)
  .attr("text-anchor", "middle")
  .attr("fill", "#be123c")
  .text("DANGER ZONE");

zoomGroup
  .append("circle")
  .attr("cx", 0)
  .attr("cy", 0)
  .attr("r", maxRadius)
  .attr("fill", "none")
  .attr("stroke", "rgba(15, 118, 110, 0.15)")
  .attr("stroke-dasharray", "4,4");
zoomGroup
  .append("text")
  .attr("class", "annotation-text")
  .attr("x", 0)
  .attr("y", -maxRadius - 10)
  .attr("text-anchor", "middle")
  .attr("fill", "#0f766e")
  .text("NORMAL ORBIT");

const tooltip = d3.select("#tooltip");

d3.csv("../data/USxIranStrikesFeb28_insiders.csv").then((data) => {
  let maxPos = 0;
  let fallbackStartDate = new Date("2026-02-01T00:00:00").getTime();

  data.forEach((d, i) => {
    d.unique_id = d.user_id + "_" + i;
    d.market_position = +d.market_position;
    d.portfolio_concentration = +d.portfolio_concentration;
    d.prior_bet_count = +d.prior_bet_count;

    // FIX: Look for target_trade_time instead of trade_date
    let parsedDate = new Date(d.target_trade_time);
    if (isNaN(parsedDate.getTime())) {
      parsedDate = new Date(fallbackStartDate + i * 1000 * 60 * 60 * 4);
    }
    d.date = parsedDate;

    if (d.market_position > maxPos) maxPos = d.market_position;

    const isFirst = d.is_first_x.trim().toLowerCase() === "true";
    const isPercent = d.Is_percent.trim().toLowerCase() === "true";
    d.is_suspicious_flag = d.is_suspicious.trim().toLowerCase() === "true";

    if (isFirst && isPercent) d.category = "Critical Risk (Both Flags)";
    else if (isFirst) d.category = "Suspicious (New Account(10 Trades))";
    else if (isPercent) d.category = "Suspicious (>80.0% Concentration)";
    else d.category = "Normal Whale";

    let vol_factor = (d.portfolio_concentration / 100.0) * 50;
    let history_factor = Math.max(0, (10 - d.prior_bet_count) / 10.0) * 50;
    let risk_score = vol_factor + history_factor;

    const radiusScale = d3
      .scaleLinear()
      .domain([0, 100])
      .range([maxRadius, 20]);
    d.r_dist = radiusScale(risk_score) + (Math.random() * 40 - 20);

    let hash = parseInt(d.user_id.substring(2, 8), 16);
    d.theta = (hash % 360) * (Math.PI / 180);
    d.x = d.r_dist * Math.cos(d.theta);
    d.y = d.r_dist * Math.sin(d.theta);
  });

  data.sort((a, b) => a.date - b.date);
  const minTime = data[0].date.getTime();
  const maxTime = data[data.length - 1].date.getTime();

  const sizeScale = d3.scaleSqrt().domain([0, maxPos]).range([5, 35]);
  let activeCategories = new Set(Object.keys(colors));

  let currentFilterTime = minTime;

  function updateStatsPanel(filteredData) {
    const totalVol = d3.sum(filteredData, (d) => d.market_position);
    const suspiciousData = filteredData.filter((d) => d.is_suspicious_flag);
    const suspiciousVol = d3.sum(suspiciousData, (d) => d.market_position);
    const pctSuspicious = totalVol > 0 ? (suspiciousVol / totalVol) * 100 : 0;

    const uniqueWhales = new Set(filteredData.map((d) => d.user_id)).size;
    const uniqueFlagged = new Set(suspiciousData.map((d) => d.user_id)).size;

    document.getElementById("stat-total-vol").innerText =
      "$" + totalVol.toLocaleString(undefined, { maximumFractionDigits: 0 });
    document.getElementById("stat-suspicious-vol").innerText =
      "$" +
      suspiciousVol.toLocaleString(undefined, { maximumFractionDigits: 0 });
    document.getElementById("stat-pct-suspicious").innerText =
      pctSuspicious.toFixed(1) + "%";
    document.getElementById("stat-total-whales").innerText = uniqueWhales;
    document.getElementById("stat-flagged").innerText = uniqueFlagged;
  }

  function updateViz() {
    const timeFilteredData = data.filter(
      (d) => d.date.getTime() <= currentFilterTime,
    );
    const visibleData = timeFilteredData.filter((d) =>
      activeCategories.has(d.category),
    );

    updateStatsPanel(visibleData);

    const nodes = zoomGroup
      .selectAll(".node")
      .data(visibleData, (d) => d.unique_id);

    nodes
      .enter()
      .append("circle")
      .attr("class", "node")
      .attr("cx", (d) => d.x)
      .attr("cy", (d) => d.y)
      .attr("r", 0)
      .attr("fill", (d) => colors[d.category])
      .attr("stroke", "white")
      .attr("stroke-width", 1.5)
      .attr("opacity", 0.9)
      .on("mouseover", function (event, d) {
        d3.select(this)
          .attr("r", sizeScale(d.market_position) + 8)
          .attr("stroke", "#223843")
          .attr("stroke-width", 3);
        zoomGroup
          .selectAll(".node")
          .filter((n) => n !== d)
          .attr("opacity", 0.1);

        tooltip.transition().duration(100).style("opacity", 1);
        tooltip
          .html(
            `
                    <div class="tooltip-title">${d.user_id.substring(0, 10)}...</div>
                    <div class="tooltip-row"><span class="tooltip-label">Date:</span> <span>${d.date.toISOString().split("T")[0]}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Category:</span> <span style="color:${colors[d.category]}; font-weight:bold;">${d.category}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Position:</span> <span>$${d.market_position.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Prior Bets:</span> <span>${d.prior_bet_count}</span></div>
                    <div class="tooltip-row"><span class="tooltip-label">Concentration:</span> <span>${d.portfolio_concentration.toFixed(1)}%</span></div>
                `,
          )
          .style("left", event.pageX - 300 + "px")
          .style("top", event.pageY - 20 + "px");
      })
      .on("mouseout", function (event, d) {
        d3.select(this)
          .attr("r", sizeScale(d.market_position))
          .attr("stroke", "white")
          .attr("stroke-width", 1.5);
        zoomGroup.selectAll(".node").attr("opacity", 0.9);
        tooltip.style("opacity", 0);
      })
      .merge(nodes)
      .attr("r", (d) => sizeScale(d.market_position));

    nodes.exit().remove();
  }

  const slider = document.getElementById("time-slider");
  const dateDisplay = document.getElementById("current-date-display");
  const playBtn = document.getElementById("play-btn");
  let playing = false;
  let playInterval;

  slider.value = 0;

  function updateFromSlider() {
    const val = +slider.value;
    currentFilterTime = minTime + (val / 100) * (maxTime - minTime);
    dateDisplay.innerText = new Date(currentFilterTime)
      .toISOString()
      .split("T")[0];
    updateViz();
  }

  slider.addEventListener("input", updateFromSlider);
  updateFromSlider();

  playBtn.addEventListener("click", () => {
    if (playing) {
      clearInterval(playInterval);
      playBtn.innerText = "▶ Play Time-Lapse";
      playing = false;
    } else {
      if (+slider.value === 100) slider.value = 0;
      playBtn.innerText = "⏸ Pause";
      playing = true;
      playInterval = setInterval(() => {
        let nextVal = +slider.value + 1;
        if (nextVal > 100) {
          clearInterval(playInterval);
          playBtn.innerText = "▶ Play Time-Lapse";
          playing = false;
          nextVal = 100;
        }
        slider.value = nextVal;
        updateFromSlider();
      }, 100);
    }
  });

  d3.selectAll(".filter-btn").on("click", function () {
    const btn = d3.select(this);
    const category = btn.attr("data-category");
    if (btn.classed("active")) {
      btn.classed("active", false);
      activeCategories.delete(category);
    } else {
      btn.classed("active", true);
      activeCategories.add(category);
    }
    updateViz();
  });
});
