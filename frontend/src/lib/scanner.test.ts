import { describe, expect, it, vi } from "vitest";

const detect = vi.fn();
vi.mock("./api", () => ({ api: { detect: (text: string) => detect(text) } }));

import { recognise } from "./scanner";

const dhl = (tracking_number: string) => ({ carrier: "de.dhl", tracking_number });

describe("recognise", () => {
  it("takes a code that is a tracking number as a whole", async () => {
    detect.mockResolvedValue({ matches: [dhl("00340434713136338502")] });
    expect(await recognise("00340434713136338502")).toEqual(dhl("00340434713136338502"));
    expect(await recognise("(00)340434713136338502")).toEqual(dhl("00340434713136338502"));
    expect(await recognise("]C100340434713136338502")).toEqual(dhl("00340434713136338502"));
    // The mark is gone before the server looks for a number.
    expect(detect).toHaveBeenLastCalledWith("00340434713136338502");
  });

  it("skips the routing code, whose digits only contain something like a number", async () => {
    detect.mockResolvedValue({ matches: [dhl("99000900057014")] });
    expect(await recognise("40327644139\u001d99000900057014")).toBeNull();
  });

  it("takes a link as it is and nothing where nothing is known", async () => {
    detect.mockResolvedValue({ matches: [dhl("JJD000390000000000001")] });
    expect(await recognise("https://www.dhl.de/x?piececode=JJD000390000000000001")).toEqual(
      dhl("JJD000390000000000001"),
    );
    detect.mockResolvedValue({ matches: [] });
    expect(await recognise("4006381333931")).toBeNull();
  });
});
