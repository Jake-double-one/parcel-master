/** Reading barcodes and QR codes from the camera. */

const FORMATS = [
  "qr_code",
  "data_matrix",
  "code_128",
  "code_39",
  "itf",
  "ean_13",
  "pdf417",
  "aztec",
] as const;

export interface Detector {
  detect(source: HTMLVideoElement): Promise<{ rawValue: string }[]>;
}

export function cameraAvailable(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

/**
 * The browser's own detector where it has one (Chrome and friends on Android).
 * Elsewhere a bundled reader is loaded on first use; nothing is fetched from
 * other servers.
 */
export async function createDetector(): Promise<Detector> {
  const native = (globalThis as { BarcodeDetector?: any }).BarcodeDetector;
  if (native) {
    try {
      const supported: string[] = await native.getSupportedFormats();
      const formats = FORMATS.filter((format) => supported.includes(format));
      if (formats.length) return new native({ formats });
    } catch {
      // Present but unusable on this device: fall through to the bundled reader.
    }
  }
  const [{ BarcodeDetector, prepareZXingModule }, wasm] = await Promise.all([
    import("barcode-detector/ponyfill"),
    import("zxing-wasm/reader/zxing_reader.wasm?url"),
  ]);
  prepareZXingModule({
    overrides: {
      locateFile: (path: string, prefix: string) =>
        path.endsWith(".wasm") ? wasm.default : prefix + path,
    },
  });
  return new BarcodeDetector({ formats: [...FORMATS] });
}
