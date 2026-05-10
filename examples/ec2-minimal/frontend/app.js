fetch(window.APP_CONFIG.apiUrl + "/hello")
  .then((response) => response.json())
  .then((data) => {
    document.getElementById("msg").innerText = data.message;
  })
  .catch((err) => {
    document.getElementById("msg").innerText = "Error: " + err;
  });
