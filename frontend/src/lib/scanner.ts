/** Reading barcodes and QR codes from the camera. */

import { api } from "./api";

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
  detect(source: HTMLVideoElement | HTMLCanvasElement): Promise<{ rawValue: string }[]>;
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

export interface Recognised {
  carrier: string;
  tracking_number: string;
}

/** Letters and digits only: how a code and a tracking number are compared. */
function bare(text: string): string {
  return text.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

/**
 * The carrier and number a scanned code stands for, or null.
 *
 * A label carries several codes. Only one that is a tracking number as a
 * whole counts: the routing code above it also contains digits that look
 * like a number, but only as a part.
 */
export async function recognise(text: string): Promise<Recognised | null> {
  const { matches } = await api.detect(text).catch(() => ({ matches: [] as Recognised[] }));
  if (!matches.length) return null;
  if (/^https?:\/\//i.test(text.trim())) return matches[0];
  // Some readers put a symbology mark such as "]C1" in front.
  const whole = bare(text.replace(/^\][A-Za-z]\d/, ""));
  return matches.find((match) => bare(match.tracking_number) === whole) ?? null;
}

/**
 * The part of a camera frame inside the aiming band, drawn onto `canvas`.
 * The video is shown cropped to its box, so the band is mapped back to the
 * frame the camera really delivers.
 */
export function aimAt(video: HTMLVideoElement, canvas: HTMLCanvasElement, band = 0.34): boolean {
  const { videoWidth, videoHeight, clientWidth, clientHeight } = video;
  if (!videoWidth || !videoHeight || !clientWidth || !clientHeight) return false;
  const scale = Math.max(clientWidth / videoWidth, clientHeight / videoHeight);
  const shownWidth = clientWidth / scale;
  const shownHeight = clientHeight / scale;
  const width = shownWidth * 0.94;
  const height = shownHeight * band;
  const left = (videoWidth - width) / 2;
  const top = (videoHeight - height) / 2;
  canvas.width = Math.round(width);
  canvas.height = Math.round(height);
  const context = canvas.getContext("2d");
  if (!context) return false;
  context.drawImage(video, left, top, width, height, 0, 0, canvas.width, canvas.height);
  return true;
}
