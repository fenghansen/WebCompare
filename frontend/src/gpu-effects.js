// A shared WebGL1 context keeps source textures resident across effect changes.
// The CPU's 8-bit LUT also supplies the shader, including ties-to-even rounding.
const vertexSource = `
attribute vec2 position;
varying vec2 uv;
void main() {
  gl_Position = vec4(position, 0.0, 1.0);
  uv = vec2((position.x + 1.0) * 0.5, (1.0 - position.y) * 0.5);
}`;
const fragmentSource = `
precision highp float;
uniform sampler2D source;
uniform sampler2D lookup;
uniform int channel;
varying vec2 uv;
float mapped(float value) {
  return texture2D(lookup, vec2((floor(value * 255.0 + 0.5) + 0.5) / 256.0, 0.5)).r;
}
void main() {
  vec4 color = texture2D(source, uv);
  vec3 result = vec3(mapped(color.r), mapped(color.g), mapped(color.b));
  if (channel == 0) result = vec3(result.r);
  if (channel == 1) result = vec3(result.g);
  if (channel == 2) result = vec3(result.b);
  gl_FragColor = vec4(result, color.a);
}`;

export class GpuEffects {
  constructor(onFailure = () => {}) {
    this.canvas = null;
    this.gl = null;
    this.program = null;
    this.buffer = null;
    this.lookup = null;
    this.textures = new Map();
    this.initialized = false;
    this.failed = false;
    this.onFailure = onFailure;
    this.onContextLost = event => { event.preventDefault(); this.dispose(false); this.onFailure(); };
  }

  initialize() {
    if (this.initialized) return !this.failed;
    this.initialized = true;
    try {
      const attributes = {
        alpha: true, premultipliedAlpha: false, antialias: false,
        depth: false, stencil: false, preserveDrawingBuffer: true,
      };
      if (typeof OffscreenCanvas === 'undefined') throw new Error('GPU bitmap transfer unavailable');
      const canvas = this.canvas = new OffscreenCanvas(1, 1);
      const gl = this.gl = canvas.getContext('webgl', attributes);
      // HTML WebGL -> Canvas2D copies regress WebKit's full-image effect performance.
      if (!gl || typeof canvas.transferToImageBitmap !== 'function') throw new Error('GPU bitmap transfer unavailable');
      canvas.addEventListener('webglcontextlost', this.onContextLost);
      const compile = (type, source) => {
        const shader = gl.createShader(type);
        if (!shader) throw new Error('Shader allocation failed');
        gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
          gl.deleteShader(shader); throw new Error('Shader compilation failed');
        }
        return shader;
      };
      const shaders = [];
      try {
        shaders.push(compile(gl.VERTEX_SHADER, vertexSource));
        shaders.push(compile(gl.FRAGMENT_SHADER, fragmentSource));
        const program = this.program = gl.createProgram();
        if (!program) throw new Error('Program allocation failed');
        shaders.forEach(shader => gl.attachShader(program, shader)); gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('Program link failed');
      } finally { shaders.forEach(shader => gl.deleteShader(shader)); }
      this.buffer = gl.createBuffer();
      this.lookup = gl.createTexture();
      if (!this.buffer || !this.lookup) throw new Error('GPU allocation failed');
      gl.useProgram(this.program); gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      const position = gl.getAttribLocation(this.program, 'position');
      gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
      gl.uniform1i(gl.getUniformLocation(this.program, 'source'), 0);
      gl.uniform1i(gl.getUniformLocation(this.program, 'lookup'), 1);
      this.channel = gl.getUniformLocation(this.program, 'channel');
      gl.disable(gl.DITHER); gl.disable(gl.BLEND); gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this.lookup); this.textureParameters();
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, 256, 1, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, new Uint8Array(256));
      this.maxSize = Math.min(gl.getParameter(gl.MAX_TEXTURE_SIZE), gl.getParameter(gl.MAX_RENDERBUFFER_SIZE));
      this.maxViewport = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
      if (gl.getError() !== gl.NO_ERROR) throw new Error('GPU initialization failed');
      return true;
    } catch { this.dispose(); return false; }
  }

  textureParameters() {
    const gl = this.gl;
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  render(key, bitmap, table, channel) {
    if (!this.initialize()) return false;
    const gl = this.gl, canvas = this.canvas;
    const width = bitmap.displayWidth ?? bitmap.width, height = bitmap.displayHeight ?? bitmap.height;
    if (width > this.maxSize || height > this.maxSize || width > this.maxViewport[0] || height > this.maxViewport[1]) return false;
    try {
      let entry = this.textures.get(key);
      if (entry && entry.bitmap !== bitmap) { this.release(key); entry = null; }
      if (!entry) {
        const source = document.createElement('canvas'); source.width = width; source.height = height;
        let pixels;
        try {
          // Match Canvas2D's decoded orientation, ICC conversion and byte quantization once.
          const ctx = source.getContext('2d', { willReadFrequently: true });
          ctx.drawImage(bitmap, 0, 0); pixels = ctx.getImageData(0, 0, width, height).data;
        } finally { source.width = 0; source.height = 0; }
        // Transparent images keep the CPU path: extra premultiplication can change low-alpha bytes.
        let opaque = true;
        for (let i = 3; i < pixels.length; i += 4) if (pixels[i] !== 255) { opaque = false; break; }
        entry = { bitmap, texture: null }; this.textures.set(key, entry);
        if (!opaque) return false;
        entry.texture = gl.createTexture();
        if (!entry.texture) throw new Error('Texture allocation failed');
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, entry.texture); this.textureParameters();
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        if (gl.getError() !== gl.NO_ERROR) throw new Error('Texture upload failed');
      }
      if (!entry.texture) return false;
      if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
      if (gl.drawingBufferWidth !== width || gl.drawingBufferHeight !== height) throw new Error('Drawing buffer limit');
      gl.viewport(0, 0, width, height); gl.useProgram(this.program);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, entry.texture);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this.lookup);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 1, gl.LUMINANCE, gl.UNSIGNED_BYTE, table);
      gl.uniform1i(this.channel, channel); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      if (gl.getError() !== gl.NO_ERROR) throw new Error('GPU draw failed');
      // Transfer the finished GPU surface instead of copying a full image through Canvas2D.
      return canvas.transferToImageBitmap();
    } catch { this.dispose(); this.onFailure(); return false; }
  }

  release(key) {
    const entry = this.textures.get(key);
    if (entry?.texture) this.gl?.deleteTexture(entry.texture);
    this.textures.delete(key);
  }

  dispose(loseContext = true) {
    this.failed = true;
    const gl = this.gl, canvas = this.canvas;
    if (gl && !gl.isContextLost()) {
      this.textures.forEach(entry => { if (entry.texture) gl.deleteTexture(entry.texture); });
      if (this.lookup) gl.deleteTexture(this.lookup);
      if (this.buffer) gl.deleteBuffer(this.buffer);
      if (this.program) gl.deleteProgram(this.program);
    }
    this.textures.clear();
    if (canvas) { canvas.removeEventListener('webglcontextlost', this.onContextLost); canvas.width = 0; canvas.height = 0; }
    if (loseContext && gl && !gl.isContextLost()) gl.getExtension('WEBGL_lose_context')?.loseContext();
    this.gl = null; this.canvas = null; this.program = null; this.buffer = null; this.lookup = null;
  }
}
