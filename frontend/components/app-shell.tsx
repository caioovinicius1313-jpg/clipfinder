import Link from "next/link";
import type { ReactNode } from "react";

type AppShellProps = {
  children: ReactNode;
};

export function AppShell({ children }: AppShellProps) {
  return (
    <main className="min-h-screen px-5 py-8 sm:px-8 lg:px-12">
      <div className="mx-auto max-w-6xl">
        <header className="mb-10 flex items-center justify-between">
          <Link href="/" className="flex items-center gap-3 text-xl font-bold tracking-tight text-slate-900">
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-indigo-600 text-white shadow-lg shadow-indigo-200">
              CF
            </span>
            <span>ClipFinder</span>
          </Link>
          <span className="rounded-full border border-indigo-100 bg-white px-3 py-1 text-sm font-medium text-indigo-700 shadow-sm">
            Matching de áudio
          </span>
        </header>
        {children}
      </div>
    </main>
  );
}
