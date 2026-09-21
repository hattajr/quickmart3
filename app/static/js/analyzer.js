/**
 * Alpine.js Component for Product Image Analyzer (BETA)
 */
document.addEventListener('alpine:init', () => {
  Alpine.data('imageAnalyzer', () => ({
    mode: 'camera', // 'camera' | 'upload'
    stream: null,
    facingMode: 'environment', // 'user' | 'environment'
    isAnalyzing: false,
    statusText: '',
    result: null,
    dragOver: false,

    init() {
      // Handle paste events when modal is active
      window.addEventListener('paste', (e) => {
        const dialog = document.getElementById('analyzer-modal');
        if (dialog && dialog.open && e.clipboardData && e.clipboardData.files.length > 0) {
          const file = e.clipboardData.files[0];
          if (file.type.startsWith('image/')) {
            this.handleFile(file);
          }
        }
      });
    },

    onOpen() {
      this.mode = 'camera';
      this.result = null;
      this.isAnalyzing = false;
      this.statusText = '';
      this.startCamera();
    },

    onClose() {
      this.stopCamera();
    },

    async startCamera() {
      this.stopCamera();
      try {
        const constraints = {
          video: {
            facingMode: { ideal: this.facingMode },
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
        };
        this.stream = await navigator.mediaDevices.getUserMedia(constraints);
        this.$refs.video.srcObject = this.stream;
      } catch (err) {
        console.error('Camera access error:', err);
        Alpine.store('toasts').show('Could not access camera. Please check permissions or upload a file.', 'error');
        this.mode = 'upload';
      }
    },

    stopCamera() {
      if (this.stream) {
        this.stream.getTracks().forEach((track) => track.stop());
        this.stream = null;
      }
    },

    async switchCamera() {
      this.facingMode = this.facingMode === 'user' ? 'environment' : 'user';
      await this.startCamera();
    },

    captureAndAnalyze() {
      const video = this.$refs.video;
      if (!video || !video.videoWidth) {
        Alpine.store('toasts').show('Camera stream not ready.', 'error');
        return;
      }

      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

      canvas.toBlob((blob) => {
        if (blob) {
          const file = new File([blob], 'camera_capture.jpg', { type: 'image/jpeg' });
          this.uploadFile(file);
        }
      }, 'image/jpeg', 0.85);
    },

    handleFile(file) {
      if (!file || !file.type.startsWith('image/')) {
        Alpine.store('toasts').show('Please select a valid image file.', 'error');
        return;
      }
      this.uploadFile(file);
    },

    async uploadFile(file) {
      this.isAnalyzing = true;
      this.statusText = 'Analyzing image with Vision LLM...';
      this.result = null;

      const formData = new FormData();
      formData.append('file', file);

      try {
        const response = await fetch('/api/identify', {
          method: 'POST',
          body: formData,
        });

        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        this.result = data;

        if (data.status === 'matched') {
          Alpine.store('toasts').show(`Matched: ${data.name}`, 'success');
        } else if (data.status === 'possible_options') {
          Alpine.store('toasts').show('Multiple options found. Please select product.', 'info');
        } else if (data.status === 'not_found') {
          Alpine.store('toasts').show('Product not found in database.', 'error');
        }
      } catch (err) {
        console.error('Image analysis failed:', err);
        Alpine.store('toasts').show('Image analysis service error. Ensure Vision LLM server is running.', 'error');
        this.result = { status: 'error', error: err.message };
      } finally {
        this.isAnalyzing = false;
      }
    },

    async addToCart(item) {
      try {
        const res = await fetch(`/api/items/${item.product_id || item.id}`);
        if (!res.ok) throw new Error('Failed to fetch item details');
        const data = await res.json();
        Alpine.store('cart').addItem(data);
        Alpine.store('toasts').show(`Added "${data.name}" to cart`, 'success');
        document.getElementById('analyzer-modal').close();
      } catch (err) {
        console.error('Error adding to cart:', err);
        Alpine.store('toasts').show('Failed to add item to cart', 'error');
      }
    },
  }));
});
