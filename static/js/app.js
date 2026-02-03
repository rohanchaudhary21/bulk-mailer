document.addEventListener("DOMContentLoaded", () => {
  const sendNow = document.querySelector('input[value="now"]');
  const schedule = document.querySelector('input[value="schedule"]');
  const timeInput = document.querySelector('input[name="time"]');

  if (!sendNow || !schedule || !timeInput) return;

  function toggleTime() {
    timeInput.disabled = sendNow.checked;
    timeInput.style.opacity = sendNow.checked ? "0.4" : "1";
  }

  sendNow.addEventListener("change", toggleTime);
  schedule.addEventListener("change", toggleTime);

  toggleTime();
});
