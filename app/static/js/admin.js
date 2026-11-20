// Admin page helpers: bottom sheets, toasts, session expiry, and invoice photo uploads.

const PHOTO_SIDE_MAX_PX = 2400;
const PHOTO_JPEG_QUALITY = 0.85;
const TOAST_MS = 3500;

function closeSheet() {
  const sheet = document.getElementById("sheet");
  if (sheet) sheet.innerHTML = "";
}
window.closeSheet = closeSheet;

let toastTimer = null;
function armToast() {
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    const toast = document.getElementById("toast");
    if (toast) toast.innerHTML = "";
  }, TOAST_MS);
}

function showToast(message, isError) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  const box = document.createElement("div");
  box.className = isError ? "toast toast-error" : "toast";
  box.setAttribute("role", "status");
  box.textContent = message;
  toast.replaceChildren(box);
  armToast();
}

document.addEventListener("htmx:oobAfterSwap", (event) => {
  if (event.detail.target && event.detail.target.id === "toast") armToast();
});

document.addEventListener("htmx:responseError", (event) => {
  // A 401 means the 8-hour admin session ended; reloading shows the login form.
  if (event.detail.xhr.status === 401) {
    window.location.reload();
    return;
  }
  // Short plain-text answers (e.g. "That invoice does not exist.") are meant for the reader.
  const xhr = event.detail.xhr;
  const text = (xhr.responseText || "").trim();
  const plain = text && text.length <= 200 && !text.startsWith("<") && !text.startsWith("{");
  showToast(plain ? text : `The server hit an error (${xhr.status}). Try again.`, true);
});

document.addEventListener("htmx:sendError", () => showToast("No connection. Check the internet and try again.", true));

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSheet();
});

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => resolve({ image, url });
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("unreadable image"));
    };
    image.src = url;
  });
}

// Shrinks a phone photo to at most PHOTO_SIDE_MAX_PX on its long side as JPEG, upright.
// The reader works on 2000 px images, so this loses nothing and keeps uploads small on mobile data.
async function shrinkPhoto(file) {
  let source;
  let width;
  let height;
  let cleanup = () => {};
  try {
    source = await createImageBitmap(file, { imageOrientation: "from-image" });
    width = source.width;
    height = source.height;
    cleanup = () => source.close();
  } catch {
    const loaded = await loadImage(file);
    source = loaded.image;
    width = loaded.image.naturalWidth;
    height = loaded.image.naturalHeight;
    cleanup = () => URL.revokeObjectURL(loaded.url);
  }
  try {
    const scale = Math.min(1, PHOTO_SIDE_MAX_PX / Math.max(width, height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", PHOTO_JPEG_QUALITY));
    if (!blob) throw new Error("could not encode the photo");
    return blob;
  } finally {
    cleanup();
  }
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("invoiceUpload", (max) => ({
    max,
    photos: [],
    busy: false,
    error: "",

    async add(event) {
      const files = Array.from(event.target.files || []);
      event.target.value = "";
      this.error = "";
      for (const file of files) {
        if (this.photos.length >= this.max) {
          this.error = `Up to ${this.max} pages at a time.`;
          break;
        }
        try {
          const blob = await shrinkPhoto(file);
          this.photos.push({ blob, url: URL.createObjectURL(blob) });
        } catch (err) {
          console.error("photo could not be opened", err);
          this.error = "This photo could not be opened. Take it again with the camera.";
        }
      }
    },

    remove(index) {
      URL.revokeObjectURL(this.photos[index].url);
      this.photos.splice(index, 1);
    },

    async send() {
      if (!this.photos.length || this.busy) return;
      this.busy = true;
      this.error = "";
      const form = new FormData();
      this.photos.forEach((photo, index) => form.append("photos", photo.blob, `page-${index + 1}.jpg`));
      try {
        const response = await fetch("/admin/invoices/uploads", { method: "POST", body: form, credentials: "same-origin" });
        if (response.status === 401) {
          window.location.reload();
          return;
        }
        const text = await response.text();
        if (!response.ok) {
          this.error = text || "The photos could not be sent. Try again.";
          return;
        }
        const list = document.getElementById("uploads");
        list.insertAdjacentHTML("afterbegin", text);
        window.htmx.process(list.firstElementChild);
        this.photos.forEach((photo) => URL.revokeObjectURL(photo.url));
        this.photos = [];
        showToast("Sent. Reading starts in a few seconds.", false);
      } catch (err) {
        console.error("upload failed", err);
        this.error = "No connection. Check the internet and try again.";
      } finally {
        this.busy = false;
      }
    },
  }));
});
