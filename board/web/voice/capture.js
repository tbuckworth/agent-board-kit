// The microphone as Gemini Live takes it (web/voice/voice.js loads this AudioWorklet): mono 16-bit little-endian PCM
// in 40 ms chunks, each posted to the page as an ArrayBuffer. A context running at a multiple of 16 kHz (48 kHz, most
// often) is brought down to 16 kHz by averaging each run of samples; any other rate (44.1 kHz) is sent as it is, which
// Gemini resamples. The first message says the rate: {rate}.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.factor = sampleRate % 16000 === 0 ? sampleRate / 16000 : 1;   // sampleRate: the context's, a worklet global
    this.rate = sampleRate / this.factor;
    this.size = Math.round(this.rate * 0.04) * 2;
    this.chunk = new DataView(new ArrayBuffer(this.size));
    this.at = 0;
    this.sum = 0;
    this.n = 0;
    this.port.postMessage({ rate: this.rate });
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i];
      if (++this.n < this.factor) continue;
      const v = Math.max(-1, Math.min(1, this.sum / this.factor));
      this.chunk.setInt16(this.at, v < 0 ? v * 0x8000 : v * 0x7fff, true);
      this.sum = 0;
      this.n = 0;
      this.at += 2;
      if (this.at === this.size) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new DataView(new ArrayBuffer(this.size));
        this.at = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm16-capture", Capture);
