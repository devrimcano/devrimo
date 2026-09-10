import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import {
  LEGAL_DOCUMENTS,
  PLACEHOLDER,
  findDocument,
  placeholderCount,
  type LegalDocument,
} from "@/lib/legal/documents";

/**
 * One renderer for every legal text.
 *
 * The words live in lib/legal/documents.ts and are unwritten on purpose; this
 * file is the part that does not need a lawyer. A draft is shown as a draft —
 * banner, unwritten spans marked in place, and `noindex`, so nobody mistakes
 * scaffolding for a notice and nothing half-written is indexed as one.
 */

export function generateStaticParams() {
  return LEGAL_DOCUMENTS.map((document) => ({ slug: document.slug }));
}

export async function generateMetadata({ params }: PageProps<"/belgeler/[slug]">): Promise<Metadata> {
  const { slug } = await params;
  const document = findDocument(slug);
  if (!document) return { title: "Devrimo" };
  return {
    title: `${document.title} · Devrimo`,
    description: document.summary,
    // A draft is scaffolding. Indexing one would publish an unfinished legal
    // text under a title that claims it is finished.
    robots: document.status === "draft" ? { index: false, follow: false } : undefined,
  };
}

/** Renders `[[ ... ]]` spans as visibly unfinished rather than as prose. */
function Paragraph({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  for (const match of text.matchAll(PLACEHOLDER)) {
    const start = match.index ?? 0;
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(
      <mark key={start} className="bg-warning/15 text-foreground border-warning/40 rounded border border-dashed px-1.5 py-0.5 text-[0.9em]">
        {match[1]}
      </mark>,
    );
    cursor = start + match[0].length;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return <p className="text-muted-foreground text-sm leading-6">{parts}</p>;
}

function DraftBanner({ document }: { document: LegalDocument }) {
  const remaining = placeholderCount(document);
  return (
    <div className="border-warning/40 bg-warning/10 flex flex-col gap-1 rounded-xl border p-4">
      <p className="text-sm font-semibold">Bu metin taslaktır</p>
      <p className="text-muted-foreground text-sm leading-6">
        Hukuki incelemeden geçmemiştir ve hiçbir rıza kaydının dayanağı olarak kullanılamaz. İşaretli{" "}
        {remaining} alan bir hukukçu tarafından doldurulacaktır. Yapı hazır, metin değil.
      </p>
    </div>
  );
}

export default async function LegalDocumentPage({ params }: PageProps<"/belgeler/[slug]">) {
  const { slug } = await params;
  const document = findDocument(slug);
  if (!document) notFound();

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-col gap-8 px-4 py-10">
      <header className="flex flex-col gap-3">
        <h1 className="text-2xl font-semibold">{document.title}</h1>
        <p className="text-muted-foreground text-sm leading-6">{document.summary}</p>
        <p className="text-muted-foreground font-mono text-xs">
          Sürüm {document.version} · Yürürlük {document.effectiveFrom}
        </p>
      </header>

      {document.status === "draft" ? <DraftBanner document={document} /> : null}

      {document.sections.map((section) => (
        <section key={section.heading} className="flex flex-col gap-2">
          <h2 className="text-base font-semibold">{section.heading}</h2>
          {section.body.map((paragraph, index) => (
            <Paragraph key={index} text={paragraph} />
          ))}
        </section>
      ))}

      <footer className="text-muted-foreground border-border border-t pt-4 text-sm">
        <Link href="/gizlilik" className="text-primary underline underline-offset-4">
          Çerezler ve gizlilik
        </Link>
      </footer>
    </main>
  );
}
