fetch("/api/stats")
  .then(res => res.json())
  .then(data => {
    document.getElementById("total").innerText = data.total;
    document.getElementById("sent").innerText = data.sent;
    document.getElementById("failed").innerText = data.failed;

    new Chart(document.getElementById("chart"), {
      type: "line",
      data: {
        labels: data.daily.map(d => d.date),
        datasets: [{
          label: "Emails Sent",
          data: data.daily.map(d => d.count),
          borderColor: "#4f46e5",
          fill: false
        }]
      }
    });
  });
