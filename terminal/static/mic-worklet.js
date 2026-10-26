/* Microphone samples to 16-bit PCM, on the audio thread.
 *
 * Kept at the context's own rate rather than resampled here: the server hands
 * the rate to PipeWire, whose resampler is a real filter, and whatever is
 * recording at the far end asks PipeWire for the rate it wants.
 *
 * Batched to 1024 frames (~20 ms at 48 kHz) so a held button is fifty frames
 * a second on the socket, not three hundred and seventy-five.
 */

class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(1024);
    this.n = 0;
    this.peak = 0;
  }

  process(inputs) {
    const samples = inputs[0] && inputs[0][0];
    if (!samples) return true;
    for (let i = 0; i < samples.length; i++) {
      const v = Math.max(-1, Math.min(1, samples[i]));
      this.peak = Math.max(this.peak, Math.abs(v));
      this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.n === this.buf.length) {
        this.port.postMessage({ pcm: this.buf.buffer, peak: this.peak }, [this.buf.buffer]);
        this.buf = new Int16Array(1024);
        this.n = 0;
        this.peak = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm16', Pcm16);
