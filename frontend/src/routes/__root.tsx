import * as React from 'react';
import { Outlet, createRootRoute } from '@tanstack/react-router';
import { TanStackRouterDevtools } from '@tanstack/react-router-devtools';
import { Separator } from '@/components/ui/separator';
import { Globe } from 'lucide-react';
import { useLocale } from '../lib/i18n-context';
import { ThemeToggle } from '../components/ThemeToggle';
import { NavigationSidebar } from '../components/NavigationSidebar';
import { DeviceProvider } from '../lib/device-context';

export const Route = createRootRoute({
  component: RootComponent,
});

export function Footer() {
  const { locale, setLocale, localeName } = useLocale();

  const toggleLocale = () => {
    setLocale(locale === 'en' ? 'zh' : 'en');
  };

  return (
    <footer className="mt-auto border-t border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950">
      <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-center gap-2 text-sm">
        <div className="flex items-center gap-2 text-slate-500 dark:text-slate-400">
          <button
            onClick={toggleLocale}
            className="hover:text-[#1d9bf0] transition-colors flex items-center gap-1"
            title="Switch language"
          >
            <Globe className="w-4 h-4" />
            {localeName}
          </button>
          <Separator
            orientation="vertical"
            className="h-4 bg-slate-200 dark:bg-slate-700"
          />
          <ThemeToggle />
        </div>
      </div>
    </footer>
  );
}

export function RootComponent() {
  return (
    <DeviceProvider>
      <div className="h-screen flex flex-col overflow-hidden">
        <div className="flex-1 flex overflow-hidden">
          <NavigationSidebar />
          <div className="flex-1 flex flex-col overflow-hidden">
            <div className="flex-1 overflow-auto">
              <Outlet />
            </div>
            <Footer />
          </div>
        </div>
        {__DEVTOOLS_ENABLED__ && (
          <TanStackRouterDevtools position="bottom-right" />
        )}
      </div>
    </DeviceProvider>
  );
}
