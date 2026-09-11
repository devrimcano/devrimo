import type { Metadata } from "next";
import { Providers } from "@/components/providers";
import { getSiteUrl } from "@/lib/env";
import "./globals.css";

// Without a metadataBase, Next resolves relative metadata URLs (the OpenGraph
// and Twitter images) against its own bind address, so production served
// `http://localhost:3000/opengraph-image` in every social preview. Setting it
// to the deployment's public origin also keeps the internal address out of the
// HTML. Left unset in a build that has no public origin, where there is nothing
// correct to point at.
const siteUrl = getSiteUrl();

export const metadata: Metadata = {
  ...(siteUrl ? { metadataBase: new URL(siteUrl) } : {}),
  title: "Devrimo",
  description: "ODTÜ öğrencileri için kişisel yapay zekâ asistanı",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="tr"
      suppressHydrationWarning
      className="h-full antialiased"
    >
      <body className="flex min-h-full flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
