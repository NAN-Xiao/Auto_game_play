import * as React from 'react';
import { Outlet, createRootRoute } from '@tanstack/react-router';
import { Separator } from '@/components/ui/separator';
import { Languages } from 'lucide-react';
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
    <footer className="mt-auto border-t app-divider bg-card/86 backdrop-blur-xl">
      <div className="mx-auto flex max-w-7xl items-center justify-center gap-2 px-5 py-3 text-sm">
        <div className="flex items-center gap-2 text-muted-foreground">
          <button
            onClick={toggleLocale}
            className="flex items-center gap-1 rounded-full px-2 py-1 transition-colors hover:text-primary"
            title="Switch language"
          >
            <Languages className="w-4 h-4" />
            {localeName}
          </button>
          <Separator orientation="vertical" className="h-4 bg-border/90" />
          <ThemeToggle />
        </div>
      </div>
    </footer>
  );
}

export function RootComponent() {
  return (
    <DeviceProvider>
      <div className="h-screen flex flex-col overflow-hidden bg-background">
        <div className="flex-1 flex overflow-hidden">
          <NavigationSidebar />
          <div className="flex-1 flex flex-col overflow-hidden">
            <div className="flex-1 overflow-auto">
              <Outlet />
            </div>
            <Footer />
          </div>
        </div>
      </div>
    </DeviceProvider>
  );
}
