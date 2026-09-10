import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The page a wrong address lands on, in the language the site is written in.
 *
 * Without this file Next serves its own: a bare "404 — This page could not be
 * found", in English, on an application whose every other word is Turkish, with
 * no way back except the browser's back button. Measured on the live site at
 * /boyle-bir-sayfa-yok, where the only link on the page belonged to the cookie
 * banner.
 *
 * It matters more than it used to. Until recently an unknown path while signed
 * out was redirected to the sign-in screen, so this page was nearly
 * unreachable; the middleware now sends anything that is not a served route
 * straight here, which was the right fix and made the stock page the thing
 * people actually see.
 *
 * Deliberately server-rendered and dependency-free: a not-found boundary that
 * needs client JavaScript to say "this does not exist" is one more thing that
 * can fail at the moment something has already failed.
 */
export default function NotFound() {
  return (
    <main
      id="main-content"
      className="campus-grid flex min-h-svh flex-col items-center justify-center gap-6 px-6 py-16 text-center"
    >
      <p className="text-primary font-mono text-sm font-semibold tracking-[0.2em]">404</p>

      <div className="flex max-w-md flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Böyle bir sayfa yok</h1>
        <p className="text-muted-foreground text-sm leading-6">
          Adres yanlış yazılmış ya da bu sayfa taşınmış olabilir. Aradığın şey muhtemelen aşağıdaki
          yerlerden birinde.
        </p>
      </div>

      <div className="flex flex-wrap items-center justify-center gap-2">
        <Link href="/" className={cn(buttonVariants({ variant: "default", size: "lg" }))}>
          Ana sayfa
        </Link>
        <Link href="/schedule" className={cn(buttonVariants({ variant: "outline", size: "lg" }))}>
          Program
        </Link>
        <Link href="/settings" className={cn(buttonVariants({ variant: "outline", size: "lg" }))}>
          Ayarlar
        </Link>
      </div>

      <Link href="/gizlilik" className="text-muted-foreground text-xs underline underline-offset-4">
        Çerezler ve gizlilik
      </Link>
    </main>
  );
}
