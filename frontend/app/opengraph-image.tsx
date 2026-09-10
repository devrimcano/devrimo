import { ImageResponse } from "next/og";

/**
 * What a shared Devrimo link looks like in a group chat.
 *
 * Until now: nothing. No `og:image` was declared, so a link pasted into
 * WhatsApp or Discord — which is how a product for students actually
 * travels — arrived as a bare URL beside a line of grey text. The card is
 * generated rather than drawn so it cannot drift from the brand: the tile, the
 * violet and the wordmark are the same values components/brand-mark.tsx and
 * app/globals.css use.
 *
 * No web font is fetched. `next/og` would need the font bytes at build time,
 * and a network fetch inside image generation is a build that fails on someone
 * else's outage; the system stack renders this weight and size honestly.
 */

export const alt = "Devrimo — ODTÜ öğrenci asistanı";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const PRIMARY = "#4a2fbd";
const SHADOW = "#301f7b";
const PAPER = "#ebe3d2";
const CARD = "#faf5e9";
const INK = "#1a1611";
const MUTED = "#645b50";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: PAPER,
          padding: 72,
          fontFamily: "system-ui, sans-serif",
        }}
      >
        {/* The mark, drawn with the same geometry as app/icon.svg. */}
        <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
          <div style={{ display: "flex", position: "relative", width: 104, height: 108 }}>
            <div
              style={{
                position: "absolute",
                top: 6,
                left: 0,
                width: 104,
                height: 102,
                borderRadius: 26,
                background: SHADOW,
                transform: "rotate(-3deg)",
              }}
            />
            <div
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: 104,
                height: 102,
                borderRadius: 26,
                background: PRIMARY,
                transform: "rotate(-3deg)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "#ffffff",
                fontSize: 54,
                fontWeight: 900,
                letterSpacing: -4,
              }}
            >
              DV
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: 40, fontWeight: 800, color: INK, letterSpacing: -1 }}>devrimo</div>
            <div style={{ fontSize: 20, fontWeight: 600, color: MUTED, letterSpacing: 4 }}>
              ODTÜ ÖĞRENCİ ASİSTANI
            </div>
          </div>
        </div>

        {/* The same promise the login page opens with. */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ fontSize: 82, fontWeight: 800, color: INK, letterSpacing: -3, lineHeight: 1.05 }}>
            ODTÜ hayatı,
          </div>
          <div style={{ fontSize: 82, fontWeight: 800, color: PRIMARY, letterSpacing: -3, lineHeight: 1.05 }}>
            biraz daha kolay.
          </div>
        </div>

        <div style={{ display: "flex", gap: 14 }}>
          {["Ders planı", "Şube kısıtları", "Kampüs bilgisi"].map((label) => (
            <div
              key={label}
              style={{
                display: "flex",
                background: CARD,
                color: MUTED,
                border: "1px solid #d8cdb8",
                borderRadius: 999,
                padding: "12px 26px",
                fontSize: 24,
                fontWeight: 600,
              }}
            >
              {label}
            </div>
          ))}
        </div>
      </div>
    ),
    size,
  );
}
