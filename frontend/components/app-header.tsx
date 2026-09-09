"use client";

import Link from "next/link";
import { BellRingIcon, CalendarDaysIcon, HomeIcon, MenuIcon, SettingsIcon, UserRoundIcon } from "lucide-react";
import { BrandMark } from "@/components/brand-mark";
import { SignOutButton } from "@/components/auth/sign-out-button";
import { LocaleSwitcher } from "@/components/locale-switcher";
import { ThemeSwitcher } from "@/components/theme-switcher";
import { useLocale } from "@/components/locale-provider";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export function AppHeader({ email }: { email?: string | null }) {
  const { pick } = useLocale();
  return (
    <header className="motion-header relative z-30 flex h-16 shrink-0 items-center justify-between gap-3 border-b bg-card/82 px-3 backdrop-blur-xl after:absolute after:inset-x-0 after:bottom-[-1px] after:h-px after:bg-gradient-to-r after:from-transparent after:via-primary/30 after:to-transparent sm:px-4">
      <Link href="/" className="group flex min-w-0 items-center gap-2 font-semibold tracking-tight outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <BrandMark className="size-8 rounded-lg shadow-[0_3px_0_color-mix(in_oklab,var(--primary)_70%,black)] transition-transform duration-200 group-hover:scale-105" />
        <span className="truncate">devrimo</span>
      </Link>

      <div className="hidden items-center gap-2 lg:flex">
        <ThemeSwitcher />
        <LocaleSwitcher />
        {email ? <span className="max-w-52 truncate px-1 text-sm text-muted-foreground">{email}</span> : null}
        <Link href="/" className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}><HomeIcon />{pick({ tr: "Ana sayfa", en: "Home" })}</Link>
        <Link href="/updates" className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}><BellRingIcon />{pick({ tr: "Güncellemeler", en: "Updates" })}</Link>
        <Link href="/schedule" className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}><CalendarDaysIcon />{pick({ tr: "Program", en: "Schedule" })}</Link>
        <Link href="/settings" className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}><SettingsIcon />{pick({ tr: "Ayarlar", en: "Settings" })}</Link>
        <SignOutButton />
      </div>

      <div className="flex items-center gap-1.5 lg:hidden">
        {/* The theme is a once-in-a-lifetime decision and it held the only
            guaranteed slot in a 375px header, while chat history was exiled to a
            floating button. It moves into the menu with the other settings. */}
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="outline" size="icon" aria-label={pick({ tr: "Hesap menüsünü aç", en: "Open account menu" })} />}>
            <MenuIcon />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-72 p-2">
            {email ? (
              <DropdownMenuGroup>
                <DropdownMenuLabel className="flex items-center gap-2 px-2 py-2">
                  <UserRoundIcon className="size-4" />
                  <span className="min-w-0 truncate">{email}</span>
                </DropdownMenuLabel>
              </DropdownMenuGroup>
            ) : null}
            <DropdownMenuSeparator />
            <DropdownMenuItem render={<Link href="/" />} className="min-h-10 px-2"><HomeIcon />{pick({ tr: "Ana sayfa", en: "Home" })}</DropdownMenuItem>
            <DropdownMenuItem render={<Link href="/updates" />} className="min-h-10 px-2"><BellRingIcon />{pick({ tr: "Güncellemeler", en: "Updates" })}</DropdownMenuItem>
            <DropdownMenuItem render={<Link href="/schedule" />} className="min-h-10 px-2"><CalendarDaysIcon />{pick({ tr: "Program", en: "Schedule" })}</DropdownMenuItem>
            <DropdownMenuItem render={<Link href="/settings" />} className="min-h-10 px-2"><SettingsIcon />{pick({ tr: "Ayarlar", en: "Settings" })}</DropdownMenuItem>
            <DropdownMenuSeparator />
            <div className="space-y-2 p-2">
              <p className="text-xs font-medium text-muted-foreground">{pick({ tr: "Görünüm", en: "Appearance" })}</p>
              <ThemeSwitcher className="h-10 w-full justify-center" />
              <p className="pt-1 text-xs font-medium text-muted-foreground">{pick({ tr: "Dil", en: "Language" })}</p>
              <LocaleSwitcher className="w-full justify-center" />
              <SignOutButton />
            </div>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
