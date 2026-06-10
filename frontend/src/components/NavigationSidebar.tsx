import React from 'react';
import { Link, useMatchRoute } from '@tanstack/react-router';
import {
  BotMessageSquare,
  CalendarClock,
  Logs,
  NotebookTabs,
  SquareTerminal,
  Workflow,
  type LucideIcon,
} from 'lucide-react';
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '@/components/ui/tooltip';
import { useTranslation } from '../lib/i18n-context';
import logoImage from '@/assets/logo.png';

interface NavigationItem {
  id: string;
  icon: LucideIcon;
  label: string;
  path: string;
}

interface NavigationSidebarProps {
  className?: string;
}

export function NavigationSidebar({ className }: NavigationSidebarProps) {
  const t = useTranslation();
  const matchRoute = useMatchRoute();

  const navigationItems: NavigationItem[] = [
    {
      id: 'chat',
      icon: BotMessageSquare,
      label: t.navigation.chat,
      path: '/chat',
    },
    {
      id: 'workflows',
      icon: Workflow,
      label: t.navigation.workflows,
      path: '/workflows',
    },
    {
      id: 'history',
      icon: NotebookTabs,
      label: t.navigation.history || '历史记录',
      path: '/history',
    },
    {
      id: 'scheduled-tasks',
      icon: CalendarClock,
      label: t.navigation.scheduledTasks || '定时任务',
      path: '/scheduled-tasks',
    },
    {
      id: 'logs',
      icon: Logs,
      label: t.navigation.logs,
      path: '/logs',
    },
    {
      id: 'terminal',
      icon: SquareTerminal,
      label: t.navigation.terminal,
      path: '/terminal',
    },
  ];

  return (
    <nav
      className={`h-full w-18 border-r app-divider bg-sidebar/94 backdrop-blur-xl ${className || ''}`}
    >
      <div className="flex flex-col items-center gap-3 px-2 py-5">
        {/* Logo at top - clickable to navigate to /chat */}
        <div className="mb-3 flex w-full justify-center border-b app-divider pb-4">
          <Tooltip>
            <TooltipTrigger asChild>
              <Link to="/chat" className="block">
                <img
                  src={logoImage}
                  alt="AutoGLM Logo"
                  className="h-11 w-11 cursor-pointer object-contain transition-opacity hover:opacity-85"
                />
              </Link>
            </TooltipTrigger>
            <TooltipContent side="right" sideOffset={8}>
              {t.navigation?.backToHome || 'Back to Home'}
            </TooltipContent>
          </Tooltip>
        </div>

        {/* Navigation items */}
        {navigationItems.map(item => {
          const Icon = item.icon;
          const isActive = matchRoute({ to: item.path });

          return (
            <Tooltip key={item.id}>
              <TooltipTrigger asChild>
                <Link
                  to={item.path}
                  className={`flex h-11 w-11 items-center justify-center rounded-2xl border transition-all duration-200 ${
                    isActive
                      ? 'border-primary/20 bg-primary/12 text-primary shadow-sm'
                      : 'border-transparent text-muted-foreground hover:border-border/70 hover:bg-accent/55 hover:text-accent-foreground'
                  }`}
                >
                  <Icon className="w-5 h-5" />
                </Link>
              </TooltipTrigger>
              <TooltipContent side="right" sideOffset={8}>
                {item.label}
              </TooltipContent>
            </Tooltip>
          );
        })}
      </div>
    </nav>
  );
}
