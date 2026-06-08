import React, { useRef, useEffect, useCallback, useState } from 'react';
import {
  Send,
  RotateCcw,
  CheckCircle2,
  AlertCircle,
  Sparkles,
  History,
  ListChecks,
  Loader2,
  Square,
  ImagePlus,
  X,
  Hand,
  ClipboardList,
} from 'lucide-react';
import { DeviceMonitor } from './DeviceMonitor';
import type {
  ExperiencePlan,
  ModelErrorDetails,
  StepTimingSummary,
  TaskImageAttachment,
  Workflow,
  HistoryRecordResponse,
} from '../api';
import {
  listWorkflows,
  listHistory,
  getHistoryRecord,
  clearHistory as clearHistoryApi,
  deleteHistoryRecord,
} from '../api';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { ScrollArea } from '@/components/ui/scroll-area';
import { useTranslation } from '../lib/i18n-context';
import { HistoryItemCard } from './HistoryItemCard';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { ImagePreview } from '@/components/ui/image-preview';
import {
  useTaskSessionConversation,
  type ObservationWindowProgress,
  type TaskConversationMessage,
} from '../hooks/useTaskSessionConversation';
import { MarkdownContent } from './MarkdownContent';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';

interface ActionPayload {
  action?: string;
  element?: [number, number];
  start?: [number, number];
  end?: [number, number];
  [key: string]: unknown;
}

interface DevicePanelProps {
  deviceId: string; // Used for API calls
  deviceSerial: string; // Used for history storage
  deviceName: string;
  deviceConnectionType?: string; // Device connection type (usb/wifi/remote)
  isConfigured: boolean;
  isVisible?: boolean; // ✅ 新增：控制视频流行为
  unlimitedStepsEnabled?: boolean;
  agentType?: string;
}

const IMAGE_ATTACHMENT_TYPES = new Set([
  'image/png',
  'image/jpeg',
  'image/webp',
]);
const MAX_IMAGE_ATTACHMENTS = 3;
const MAX_IMAGE_ATTACHMENT_BYTES = 5 * 1024 * 1024;
const EXPERIENCE_CONFIRMATION_COMMANDS = new Set([
  '开始',
  '开始执行',
  '确认',
  '确认开始',
  '确认执行',
  '确认并开始',
  '确认无误直接开始',
  '执行',
  'start',
  'go',
  'ok',
  'yes',
]);

function isExperienceConfirmationInput(value: string): boolean {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[\s，。！？、,.!?]+/g, '');
  return EXPERIENCE_CONFIRMATION_COMMANDS.has(normalized);
}

function getExperiencePlanFromMessage(
  message: TaskConversationMessage
): ExperiencePlan | null {
  const plan = message.metadata?.plan;
  if (!plan || typeof plan !== 'object') {
    return null;
  }
  return plan as ExperiencePlan;
}

function getExperienceQuestionFromMessage(
  message: TaskConversationMessage
): string | null {
  const question = message.metadata?.question;
  return typeof question === 'string' && question.trim() ? question : null;
}

function memoryPolicyLabel(policy: ExperiencePlan['memory_policy']): string {
  switch (policy) {
    case 'independent_items':
      return '独立对象：每轮只看当前对象，历史用于最终报告';
    case 'stateful_flow':
      return '连续状态：保留目标、进度和上一步结果';
    default:
      return '混合：当前对象独立分析，同时保留必要进度';
  }
}

function ObservationWindowCard({
  window,
}: {
  window: ObservationWindowProgress;
}) {
  const capturedCount = window.samples.length;
  const complete = window.completed || capturedCount >= window.sampleCount;
  const intervalLabel = Number.isInteger(window.intervalSeconds)
    ? String(window.intervalSeconds)
    : window.intervalSeconds.toFixed(1);

  return (
    <div className="rounded-2xl rounded-tl-sm border border-cyan-200 bg-cyan-50 px-4 py-3 text-sm text-slate-700 dark:border-cyan-900/60 dark:bg-cyan-950/20 dark:text-slate-200">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-cyan-700 dark:text-cyan-300">
          <div className="flex h-6 w-6 items-center justify-center rounded-full bg-cyan-500/10">
            {complete ? (
              <CheckCircle2 className="h-3.5 w-3.5" />
            ) : (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            )}
          </div>
          <span className="font-medium">直接截帧</span>
        </div>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          第 {window.step} 轮 · {capturedCount}/{window.sampleCount} 张 · 间隔{' '}
          {intervalLabel} 秒
        </span>
      </div>
      <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
        {window.message ||
          (complete
            ? `已采集 ${capturedCount} 张截图，开始一次性多模态综合分析。`
            : `正在按配置直接截帧，不逐张调用模型。`)}
      </p>
      {window.samples.length > 0 && (
        <div className="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-5">
          {window.samples.map(sample => (
            <div key={sample.index} className="space-y-1">
              {sample.screenshot ? (
                <ImagePreview
                  src={`data:image/png;base64,${sample.screenshot}`}
                  alt={`观察截图 ${sample.index}`}
                  maxHeight="180px"
                />
              ) : (
                <div className="aspect-[9/16] rounded-lg border border-dashed border-cyan-200 dark:border-cyan-800" />
              )}
              <div className="text-center text-[11px] text-slate-500 dark:text-slate-400">
                {sample.index}/{window.sampleCount}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ExperiencePlanMessageCard({
  message,
  isLatest,
  isConfirmed,
  canConfirm,
  onConfirm,
}: {
  message: TaskConversationMessage;
  isLatest: boolean;
  isConfirmed: boolean;
  canConfirm: boolean;
  onConfirm: () => void;
}) {
  const plan = getExperiencePlanFromMessage(message);
  const question = getExperienceQuestionFromMessage(message);

  if (!isLatest) {
    return (
      <div className="max-w-[85%] rounded-2xl rounded-tl-sm border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-900/60 dark:text-slate-400">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-4 w-4" />
          <span className="font-medium">历史草案已失效</span>
        </div>
      </div>
    );
  }

  if (!plan) {
    return (
      <div className="max-w-[85%] rounded-2xl rounded-tl-sm border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-slate-700 dark:border-sky-900/60 dark:bg-sky-950/20 dark:text-slate-200">
        <div className="flex items-center gap-2 text-sky-700 dark:text-sky-300">
          <ClipboardList className="h-4 w-4" />
          <span className="font-medium">草案已更新</span>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-[85%] rounded-2xl rounded-tl-sm border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-slate-700 dark:border-sky-900/60 dark:bg-sky-950/20 dark:text-slate-200">
      <div className="flex items-center gap-2 text-sky-700 dark:text-sky-300">
        <ClipboardList className="h-4 w-4" />
        <span className="font-medium">
          {isConfirmed ? '已确认体验委托' : '体验任务草案'}
        </span>
      </div>
      <div className="mt-3 space-y-3">
        <div>
          <p className="font-medium">体验目标</p>
          <p>{plan.execution_goal}</p>
        </div>
        <div>
          <p className="font-medium">重点观察</p>
          <p>{plan.observation_targets.join(' / ')}</p>
        </div>
        <div>
          <p className="font-medium">分析方法</p>
          <p>{plan.analysis_lenses.join(' / ')}</p>
        </div>
        <div>
          <p className="font-medium">评估维度</p>
          <p>{plan.evaluation_dimensions.join(' / ')}</p>
        </div>
        <div>
          <p className="font-medium">最终输出</p>
          <p>{plan.report_request}</p>
        </div>
        <div>
          <p className="font-medium">停止条件</p>
          <p>{plan.stop_conditions.join(' / ')}</p>
        </div>
        <div>
          <p className="font-medium">取证策略</p>
          <p>{plan.sampling_strategy.join(' / ')}</p>
        </div>
        <div>
          <p className="font-medium">记忆方式</p>
          <p>{memoryPolicyLabel(plan.memory_policy)}</p>
        </div>
        {question && !isConfirmed && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-amber-700 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-300">
            {question}
          </div>
        )}
        {canConfirm && (
          <div className="pt-1 text-center text-sm text-slate-500 dark:text-slate-400">
            可以继续修改或者
            <a
              href="#"
              onClick={event => {
                event.preventDefault();
                onConfirm();
              }}
              className="ml-1 font-medium text-sky-700 underline underline-offset-4 transition-colors hover:text-sky-900 dark:text-sky-300 dark:hover:text-sky-100"
            >
              直接执行
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

function readImageAttachment(file: File): Promise<TaskImageAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : '';
      const commaIndex = result.indexOf(',');
      if (commaIndex === -1) {
        reject(new Error('图片格式无效'));
        return;
      }
      resolve({
        mime_type: file.type,
        data: result.slice(commaIndex + 1),
        name: file.name || null,
      });
    };
    reader.readAsDataURL(file);
  });
}

function getStepSummary(thinking: string | undefined, action: unknown): string {
  if (action && typeof action === 'object') {
    const actionRecord = action as Record<string, unknown>;
    const metadata = actionRecord['_metadata'];
    const actionMessage = actionRecord['message'];

    if (
      typeof actionMessage === 'string' &&
      actionMessage.trim().toUpperCase().startsWith('OBJECT_SUMMARY:')
    ) {
      const summary = actionMessage
        .trim()
        .replace(/^OBJECT_SUMMARY:\s*/i, '')
        .trim();
      return summary ? `本轮小结：\n${summary}` : actionMessage.trim();
    }

    if (metadata === 'finish') {
      const finishMessage = actionRecord['message'];
      if (
        typeof finishMessage === 'string' &&
        finishMessage.trim().length > 0
      ) {
        return `Finish: ${finishMessage}`;
      }
      return 'Finish task';
    }

    const actionName = actionRecord['action'];
    if (typeof actionName === 'string' && actionName.trim().length > 0) {
      return `Action: ${actionName}`;
    }
  }

  if (thinking && thinking.trim().length > 0) {
    return thinking;
  }

  return 'Action executed';
}

function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${Math.round(ms)}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}

function getTimingChips(
  timings: StepTimingSummary | undefined
): Array<{ label: string; value: string }> {
  if (!timings) {
    return [];
  }

  const chips = [
    { label: 'Total', value: formatDuration(timings.total_duration_ms) },
    { label: 'Model', value: formatDuration(timings.llm_duration_ms) },
  ];

  if (timings.screenshot_duration_ms > 0) {
    chips.push({
      label: 'Shot',
      value: formatDuration(timings.screenshot_duration_ms),
    });
  }

  if (timings.current_app_duration_ms > 0) {
    chips.push({
      label: 'App',
      value: formatDuration(timings.current_app_duration_ms),
    });
  }

  if (timings.execute_action_duration_ms > 0) {
    chips.push({
      label: 'Action',
      value: formatDuration(timings.execute_action_duration_ms),
    });
  }

  if (timings.sleep_duration_ms > 0) {
    chips.push({
      label: 'Sleep',
      value: formatDuration(timings.sleep_duration_ms),
    });
  }

  return chips;
}

function getObservationWindowForStep(
  windows: ObservationWindowProgress[] | undefined,
  step: number
): ObservationWindowProgress | undefined {
  return windows?.find(window => window.step === step);
}

function getAnalysisTitle(
  step: number,
  observationWindow: ObservationWindowProgress | undefined
): string {
  return observationWindow ? `第 ${step} 轮综合分析` : `Step ${step}`;
}

function formatModelErrorDetails(details: ModelErrorDetails): string {
  const ordered: Record<string, unknown> = {};
  [
    'kind',
    'exception_type',
    'message',
    'status_code',
    'request_id',
    'model_name',
    'base_url',
    'call_site',
    'response_headers',
    'response_body',
    'traceback',
  ].forEach(key => {
    if (details[key] !== undefined && details[key] !== null) {
      ordered[key] = details[key];
    }
  });

  Object.entries(details).forEach(([key, value]) => {
    if (!(key in ordered) && value !== undefined && value !== null) {
      ordered[key] = value;
    }
  });

  return JSON.stringify(ordered, null, 2);
}

export function DevicePanel({
  deviceId,
  deviceSerial,
  deviceName,
  deviceConnectionType,
  isConfigured,
  isVisible = true, // ✅ 新增：默认 true 向后兼容
  unlimitedStepsEnabled = false,
  agentType,
}: DevicePanelProps) {
  const t = useTranslation();
  const [input, setInput] = useState('');
  const [interactionMode, setInteractionMode] = useState<'chat' | 'experience'>(
    'chat'
  );
  const [attachments, setAttachments] = useState<TaskImageAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [isDraggingAttachment, setIsDraggingAttachment] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // ✅ 移除 initialized 状态，依赖后端自动初始化
  // const [initialized, setInitialized] = useState(false);
  const [showHistoryPopover, setShowHistoryPopover] = useState(false);
  const [historyItems, setHistoryItems] = useState<HistoryRecordResponse[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [showWorkflowPopover, setShowWorkflowPopover] = useState(false);
  const {
    messages,
    setMessages,
    loading,
    aborting,
    waitingForDevice,
    waitingForUserInteraction,
    interactionPrompt,
    error,
    sessionReady,
    experienceStage,
    experiencePlan,
    sendMessage,
    resetConversation,
    abortConversation,
    confirmExperiencePlan,
  } = useTaskSessionConversation({
    deviceId,
    deviceSerial,
    sessionStorageKey: `autoglm:classic-session:${deviceSerial}`,
    agentType,
  });
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const prevMessageCountRef = useRef(0);
  const prevMessageSigRef = useRef<string | null>(null);
  // The chat follows the latest message by default. Only a deliberate upward
  // scroll by the user turns this off — programmatic re-pins and content that
  // grows underneath (e.g. screenshots finishing decode) must never flip it,
  // otherwise the stale scroll events they emit would strand the view.
  const isAtBottomRef = useRef(true);
  // Timestamp of the last programmatic scroll-to-bottom. Scroll events that
  // land within this window are the echo of our own pinning (or of the layout
  // settling afterwards) and are ignored, not treated as the user leaving.
  const lastPinTimeRef = useRef(0);
  // Last observed scrollTop. Used to tell a real upward scroll (scrollTop
  // decreases) apart from content growing underneath (scrollTop stays put).
  const lastScrollTopRef = useRef(0);
  const [showNewMessageNotice, setShowNewMessageNotice] = useState(false);

  // The actual scrollable element lives inside the Radix ScrollArea.
  const getScrollViewport = useCallback(
    () =>
      (scrollAreaRef.current?.querySelector(
        '[data-slot="scroll-area-viewport"]'
      ) as HTMLDivElement | null) ?? null,
    []
  );

  const pinToBottom = useCallback(
    (behavior: 'auto' | 'smooth' = 'auto') => {
      const viewport = getScrollViewport();
      if (!viewport) return;
      lastPinTimeRef.current = performance.now();
      viewport.scrollTo({ top: viewport.scrollHeight, behavior });
    },
    [getScrollViewport]
  );

  // Step screenshots load asynchronously and grow the content after the
  // streaming effect already scrolled, which would otherwise leave the chat
  // parked a few hundred pixels above the latest message. A ResizeObserver
  // re-pins the view to the bottom whenever the content height changes while
  // the user is still following along.
  useEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    const observer = new ResizeObserver(() => {
      if (isAtBottomRef.current) {
        pinToBottom();
      }
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [pinToBottom]);

  // ✅ 移除 handleInit 函数，不再需要显式初始化
  // Agent 会在首次发送消息时自动初始化

  // ✅ 移除自动初始化 useEffect，不再需要

  // Load history items when popover opens
  useEffect(() => {
    if (showHistoryPopover) {
      const loadItems = async () => {
        try {
          const data = await listHistory(deviceSerial, 20, 0, 'classic');
          setHistoryItems(data.records);
        } catch (error) {
          console.error('Failed to load history:', error);
          setHistoryItems([]);
        }
      };
      loadItems();
    }
  }, [showHistoryPopover, deviceSerial]);

  const handleSelectHistory = (record: HistoryRecordResponse) => {
    void (async () => {
      let selectedRecord = record;
      try {
        selectedRecord = await getHistoryRecord(deviceSerial, record.id);
      } catch (error) {
        console.error('Failed to load history record detail:', error);
      }

      // Convert backend messages to frontend Message format
      const newMessages: TaskConversationMessage[] = [];

      // Find user message from record
      const userMsg = selectedRecord.messages.find(m => m.role === 'user');
      if (userMsg) {
        newMessages.push({
          id: `${selectedRecord.id}-user`,
          role: 'user',
          content: userMsg.content || selectedRecord.task_text,
          timestamp: new Date(userMsg.timestamp),
          attachments: userMsg.attachments || [],
        });
      } else {
        // Fallback to task_text if no user message
        newMessages.push({
          id: `${selectedRecord.id}-user`,
          role: 'user',
          content: selectedRecord.task_text,
          timestamp: new Date(selectedRecord.start_time),
        });
      }

      // Collect thinking and actions from assistant messages
      const thinkingList: string[] = [];
      const actionsList: Record<string, unknown>[] = [];
      const screenshotsList: (string | undefined)[] = [];
      selectedRecord.messages
        .filter(m => m.role === 'assistant')
        .forEach(m => {
          if (m.thinking) thinkingList.push(m.thinking);
          if (m.action) actionsList.push(m.action);
          // Extract screenshot directly or from loosely typed object
          const recordData = m as unknown as { screenshot?: string };
          screenshotsList.push(recordData.screenshot);
        });

      // Create agent message
      const agentMessage: TaskConversationMessage = {
        id: `${selectedRecord.id}-agent`,
        role: 'assistant',
        content: selectedRecord.final_message,
        timestamp: selectedRecord.end_time
          ? new Date(selectedRecord.end_time)
          : new Date(selectedRecord.start_time),
        steps: selectedRecord.steps,
        success: selectedRecord.success,
        thinking: thinkingList,
        actions: actionsList,
        screenshots: screenshotsList,
        stepTimings: selectedRecord.step_timings,
        isStreaming: false,
      };
      newMessages.push(agentMessage);

      setMessages(newMessages);

      // Reset previous message tracking refs to match the loaded history
      prevMessageCountRef.current = newMessages.length;
      prevMessageSigRef.current = [
        agentMessage.id,
        agentMessage.content?.length ?? 0,
        agentMessage.currentThinking?.length ?? 0,
        agentMessage.thinking
          ? JSON.stringify(agentMessage.thinking).length
          : 0,
        agentMessage.steps ?? '',
        agentMessage.isStreaming ? 1 : 0,
      ].join('|');

      setShowNewMessageNotice(false);
      isAtBottomRef.current = true;
      setShowHistoryPopover(false);
    })();
  };

  const handleClearHistory = async () => {
    if (confirm(t.history.clearAllConfirm)) {
      try {
        await clearHistoryApi(deviceSerial);
        setHistoryItems([]);
      } catch (error) {
        console.error('Failed to clear history:', error);
      }
    }
  };

  const handleDeleteItem = async (itemId: string) => {
    try {
      await deleteHistoryRecord(deviceSerial, itemId);
      // 从列表中移除已删除的项
      setHistoryItems(prev => prev.filter(item => item.id !== itemId));
    } catch (error) {
      console.error('Failed to delete history item:', error);
    }
  };

  // Note: Configuration is now managed entirely by backend ConfigManager.
  // If user updates config via Settings, they need to manually re-initialize agents.

  const addImageFiles = useCallback(
    async (files: File[]) => {
      const imageFiles = files.filter(file =>
        IMAGE_ATTACHMENT_TYPES.has(file.type)
      );
      if (imageFiles.length === 0) {
        return;
      }

      if (attachments.length + imageFiles.length > MAX_IMAGE_ATTACHMENTS) {
        setAttachmentError('最多只能附加 3 张图片');
        return;
      }

      const tooLargeFile = imageFiles.find(
        file => file.size > MAX_IMAGE_ATTACHMENT_BYTES
      );
      if (tooLargeFile) {
        setAttachmentError('单张图片不能超过 5 MiB');
        return;
      }

      try {
        const nextAttachments = await Promise.all(
          imageFiles.map(file => readImageAttachment(file))
        );
        setAttachments(current => [...current, ...nextAttachments]);
        setAttachmentError(null);
      } catch (readError) {
        setAttachmentError(
          readError instanceof Error ? readError.message : '读取图片失败'
        );
      }
    },
    [attachments.length]
  );

  const handleFileInputChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files || []);
      void addImageFiles(files);
      event.target.value = '';
    },
    [addImageFiles]
  );

  const handlePaste = useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const files = Array.from(event.clipboardData.files || []);
      const hasImages = files.some(file =>
        IMAGE_ATTACHMENT_TYPES.has(file.type)
      );
      if (!hasImages) {
        return;
      }
      event.preventDefault();
      void addImageFiles(files);
    },
    [addImageFiles]
  );

  const handleDragOver = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (
        Array.from(event.dataTransfer.items || []).some(item =>
          IMAGE_ATTACHMENT_TYPES.has(item.type)
        )
      ) {
        event.preventDefault();
        setIsDraggingAttachment(true);
      }
    },
    []
  );

  const handleDragLeave = useCallback(() => {
    setIsDraggingAttachment(false);
  }, []);

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      const files = Array.from(event.dataTransfer.files || []);
      const hasImages = files.some(file =>
        IMAGE_ATTACHMENT_TYPES.has(file.type)
      );
      if (!hasImages) {
        return;
      }
      event.preventDefault();
      setIsDraggingAttachment(false);
      void addImageFiles(files);
    },
    [addImageFiles]
  );

  const removeAttachment = useCallback((index: number) => {
    setAttachments(current => current.filter((_, idx) => idx !== index));
  }, []);

  const handleSend = useCallback(async () => {
    if (
      interactionMode === 'experience' &&
      experiencePlan &&
      (experienceStage === 'asking' ||
        experienceStage === 'awaiting_confirmation') &&
      isExperienceConfirmationInput(input)
    ) {
      const didConfirm = await confirmExperiencePlan();
      if (didConfirm) {
        setInput('');
        setAttachments([]);
        setAttachmentError(null);
      }
      return;
    }

    const didSend = await sendMessage(input, attachments, {
      experienceMode: interactionMode === 'experience',
    });
    if (didSend) {
      setInput('');
      if (interactionMode === 'chat') {
        setAttachments([]);
        setAttachmentError(null);
      }
    }
  }, [
    attachments,
    confirmExperiencePlan,
    experiencePlan,
    experienceStage,
    input,
    interactionMode,
    sendMessage,
  ]);

  const handleReset = useCallback(async () => {
    await resetConversation();
    setShowNewMessageNotice(false);
    isAtBottomRef.current = true;
    prevMessageCountRef.current = 0;
    prevMessageSigRef.current = null;
    setAttachments([]);
    setAttachmentError(null);
  }, [resetConversation]);

  const handleAbortChat = useCallback(async () => {
    await abortConversation();
  }, [abortConversation]);

  const handleConfirmExperience = useCallback(async () => {
    const didConfirm = await confirmExperiencePlan();
    if (didConfirm) {
      setInput('');
      setAttachments([]);
      setAttachmentError(null);
    }
  }, [confirmExperiencePlan]);

  useEffect(() => {
    const latest = messages[messages.length - 1];
    const thinkingSignature = latest?.thinking
      ? JSON.stringify(latest.thinking).length
      : 0;
    const latestSignature = latest
      ? [
          latest.id,
          latest.content?.length ?? 0,
          latest.currentThinking?.length ?? 0,
          thinkingSignature,
          latest.steps ?? '',
          latest.isStreaming ? 1 : 0,
        ].join('|')
      : null;

    const isNewMessage = messages.length > prevMessageCountRef.current;
    const hasLatestChanged =
      latestSignature !== prevMessageSigRef.current && messages.length > 0;

    prevMessageCountRef.current = messages.length;
    prevMessageSigRef.current = latestSignature;

    if (isAtBottomRef.current) {
      pinToBottom();
      const frameId = requestAnimationFrame(() => {
        setShowNewMessageNotice(false);
      });
      return () => cancelAnimationFrame(frameId);
    }

    if (messages.length === 0) {
      const frameId = requestAnimationFrame(() => {
        setShowNewMessageNotice(false);
      });
      return () => cancelAnimationFrame(frameId);
    }

    if (isNewMessage || hasLatestChanged) {
      const frameId = requestAnimationFrame(() => {
        setShowNewMessageNotice(true);
      });
      return () => cancelAnimationFrame(frameId);
    }
  }, [messages, pinToBottom]);

  // Load workflows
  useEffect(() => {
    const loadWorkflows = async () => {
      try {
        const data = await listWorkflows();
        setWorkflows(data.workflows);
      } catch (error) {
        console.error('Failed to load workflows:', error);
      }
    };
    loadWorkflows();
  }, []);

  const handleExecuteWorkflow = (workflow: Workflow) => {
    setInput(workflow.text);
    setShowWorkflowPopover(false);
  };

  const handleMessagesScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    const scrollTop = target.scrollTop;
    const prevScrollTop = lastScrollTopRef.current;
    lastScrollTopRef.current = scrollTop;

    // Ignore the scroll events caused by our own re-pinning and by the
    // re-layout that late-loading content (screenshots) triggers right after.
    if (performance.now() - lastPinTimeRef.current < 150) return;

    const distanceFromBottom =
      target.scrollHeight - scrollTop - target.clientHeight;
    // A generous band so a few hundred pixels of late-loading content between
    // streaming updates doesn't break following.
    if (distanceFromBottom < 150) {
      isAtBottomRef.current = true;
      setShowNewMessageNotice(false);
      return;
    }
    // Far from the bottom: only treat it as the user opting out if they
    // actually scrolled upward. Content growing or a programmatic re-pin keeps
    // (or raises) scrollTop, so the stale events they emit can't trip this.
    if (scrollTop < prevScrollTop - 4) {
      isAtBottomRef.current = false;
    }
  };

  const handleScrollToLatest = () => {
    isAtBottomRef.current = true;
    pinToBottom();
    setShowNewMessageNotice(false);
  };

  const handleInputKeyDown = (
    event: React.KeyboardEvent<HTMLTextAreaElement>
  ) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      handleSend();
    }
  };

  const latestExperiencePlanMessageId =
    [...messages]
      .reverse()
      .find(
        message =>
          message.role === 'assistant' &&
          message.eventType === 'experience_plan'
      )?.id ?? null;
  const canConfirmLatestExperiencePlan =
    interactionMode === 'experience' &&
    experiencePlan !== null &&
    (experienceStage === 'asking' ||
      experienceStage === 'awaiting_confirmation');

  return (
    <div className="flex-1 flex gap-4 p-4 items-stretch justify-center min-h-0">
      {/* Chat area - takes remaining space */}
      <Card className="flex-1 flex flex-col min-h-0 max-w-2xl overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-slate-200 dark:border-slate-800">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[#1d9bf0]/10">
              <Sparkles className="h-5 w-5 text-[#1d9bf0]" />
            </div>
            <div className="group">
              <div className="flex items-center gap-1">
                <h2 className="font-bold text-slate-900 dark:text-slate-100">
                  {deviceName}
                </h2>
              </div>
              <p className="text-xs text-slate-500 dark:text-slate-400 font-mono">
                {deviceId}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {loading && unlimitedStepsEnabled && (
              <Badge
                variant="secondary"
                className="bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
              >
                无限步数模式
              </Badge>
            )}
            {/* History button with Popover */}
            <Popover
              open={showHistoryPopover}
              onOpenChange={setShowHistoryPopover}
            >
              <PopoverTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 rounded-full text-slate-400 hover:text-slate-600 dark:text-slate-500 dark:hover:text-slate-300"
                  title={t.history.title}
                >
                  <History className="h-4 w-4" />
                </Button>
              </PopoverTrigger>

              <PopoverContent className="w-96 p-0" align="end" sideOffset={8}>
                {/* Header */}
                <div className="flex items-center justify-between p-4 border-b border-slate-200 dark:border-slate-800">
                  <h3 className="font-semibold text-sm text-slate-900 dark:text-slate-100">
                    {t.history.title}
                  </h3>
                  {historyItems.length > 0 && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={handleClearHistory}
                      className="h-7 text-xs"
                    >
                      {t.history.clearAll}
                    </Button>
                  )}
                </div>

                {/* Scrollable content */}
                <ScrollArea className="h-[400px]">
                  <div className="p-4 space-y-2">
                    {historyItems.length > 0 ? (
                      historyItems.map(item => (
                        <HistoryItemCard
                          key={item.id}
                          item={item}
                          onSelect={handleSelectHistory}
                          onDelete={handleDeleteItem}
                        />
                      ))
                    ) : (
                      <div className="text-center py-8">
                        <History className="h-12 w-12 text-slate-300 dark:text-slate-700 mx-auto mb-3" />
                        <p className="text-sm font-medium text-slate-900 dark:text-slate-100">
                          {t.history.noHistory}
                        </p>
                        <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                          {t.history.noHistoryDescription}
                        </p>
                      </div>
                    )}
                  </div>
                </ScrollArea>
              </PopoverContent>
            </Popover>

            {!isConfigured && (
              <Badge variant="warning">
                <AlertCircle className="w-3 h-3 mr-1" />
                {t.devicePanel.noConfig}
              </Badge>
            )}

            <Button
              variant="ghost"
              size="icon"
              onClick={handleReset}
              className="h-8 w-8 rounded-full text-slate-400 hover:text-slate-600 dark:text-slate-500 dark:hover:text-slate-300"
              title={t.devicePanel?.resetChat || 'Reset Chat'}
            >
              <RotateCcw className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Error message */}
        {(error || attachmentError) && (
          <div className="mx-4 mt-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-xl text-sm text-red-600 dark:text-red-400 flex items-center gap-2">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            {error || attachmentError}
          </div>
        )}

        <div className="px-4 pt-4">
          <Tabs
            className="mx-auto w-full max-w-sm"
            value={interactionMode}
            onValueChange={value =>
              setInteractionMode(value === 'experience' ? 'experience' : 'chat')
            }
          >
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="chat">普通对话</TabsTrigger>
              <TabsTrigger value="experience">体验任务</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>

        {/* Messages */}
        <div className="flex-1 min-h-0 relative">
          <ScrollArea
            ref={scrollAreaRef}
            className="h-full"
            data-testid="chat-scroll-container"
            onScroll={handleMessagesScroll}
          >
            <div className="p-4" ref={contentRef}>
              {messages.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-center min-h-[calc(100%-1rem)]">
                  <div className="flex h-16 w-16 items-center justify-center rounded-full bg-slate-100 dark:bg-slate-800 mb-4">
                    <Sparkles className="h-8 w-8 text-slate-400" />
                  </div>
                  <p className="font-medium text-slate-900 dark:text-slate-100">
                    {t.devicePanel.readyToHelp}
                  </p>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                    {t.devicePanel.describeTask}
                  </p>
                </div>
              ) : (
                messages.map(message => (
                  <div
                    key={message.id}
                    className={`flex ${
                      message.role === 'user' ? 'justify-end' : 'justify-start'
                    }`}
                  >
                    {message.role === 'assistant' &&
                    message.eventType === 'experience_plan' ? (
                      <ExperiencePlanMessageCard
                        message={message}
                        isLatest={message.id === latestExperiencePlanMessageId}
                        isConfirmed={
                          message.id === latestExperiencePlanMessageId &&
                          (experienceStage === 'running' ||
                            experienceStage === 'reported')
                        }
                        canConfirm={
                          message.id === latestExperiencePlanMessageId &&
                          canConfirmLatestExperiencePlan
                        }
                        onConfirm={handleConfirmExperience}
                      />
                    ) : message.role === 'assistant' ? (
                      <div className="max-w-[85%] space-y-3">
                        {Array.from(
                          {
                            length: Math.max(
                              message.thinking?.length || 0,
                              message.actions?.length || 0
                            ),
                          },
                          (_, idx) => idx
                        ).map(idx => {
                          const stepThinking = message.thinking?.[idx];
                          const stepAction = message.actions?.[idx];
                          const stepScreenshot = message.screenshots?.[idx];
                          const stepTimings = message.stepTimings?.[idx];
                          const stepSummary = getStepSummary(
                            stepThinking,
                            stepAction
                          );

                          return (
                            <div key={idx} className="space-y-3">
                              {(() => {
                                const stepNumber =
                                  message.stepNumbers?.[idx] ?? idx + 1;
                                const observationWindow =
                                  getObservationWindowForStep(
                                    message.observationWindows,
                                    stepNumber
                                  );
                                return (
                                  <>
                                    {observationWindow && (
                                      <ObservationWindowCard
                                        window={observationWindow}
                                      />
                                    )}
                                    <div className="bg-slate-100 dark:bg-slate-800 rounded-2xl rounded-tl-sm px-4 py-3">
                                      <div className="flex items-center gap-2 mb-2">
                                        <div className="flex h-6 w-6 items-center justify-center rounded-full bg-[#1d9bf0]/10">
                                          <Sparkles className="h-3 w-3 text-[#1d9bf0]" />
                                        </div>
                                        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">
                                          {getAnalysisTitle(
                                            stepNumber,
                                            observationWindow
                                          )}
                                        </span>
                                      </div>
                                      <p className="text-sm whitespace-pre-wrap text-slate-700 dark:text-slate-300">
                                        {stepSummary}
                                      </p>

                                      {stepTimings && (
                                        <div className="mt-3 flex flex-wrap gap-2">
                                          {getTimingChips(stepTimings).map(
                                            chip => (
                                              <Badge
                                                key={`${idx}-${chip.label}`}
                                                variant="secondary"
                                                className="font-mono text-[11px]"
                                              >
                                                {chip.label} {chip.value}
                                              </Badge>
                                            )
                                          )}
                                        </div>
                                      )}

                                      {!observationWindow && stepScreenshot && (
                                        <div className="mt-3">
                                          <ImagePreview
                                            src={`data:image/png;base64,${stepScreenshot}`}
                                            alt={`Step ${idx + 1}`}
                                            maxHeight="350px"
                                          >
                                            {stepAction &&
                                              (() => {
                                                const parsedAction =
                                                  stepAction as ActionPayload;
                                                const actionName =
                                                  parsedAction.action;

                                                if (
                                                  actionName &&
                                                  [
                                                    'Tap',
                                                    'Double Tap',
                                                    'Long Press',
                                                  ].includes(actionName)
                                                ) {
                                                  const element =
                                                    parsedAction.element;
                                                  if (
                                                    Array.isArray(element) &&
                                                    element.length === 2
                                                  ) {
                                                    const left = `${(Math.max(0, Math.min(element[0], 1000)) / 1000) * 100}%`;
                                                    const top = `${(Math.max(0, Math.min(element[1], 1000)) / 1000) * 100}%`;
                                                    return (
                                                      <div
                                                        className="absolute w-8 h-8 rounded-full border-[3px] border-red-500 bg-red-500/20 transform -translate-x-1/2 -translate-y-1/2 pointer-events-none animate-pulse shadow-[0_0_8px_rgba(239,68,68,0.6)]"
                                                        style={{ left, top }}
                                                      />
                                                    );
                                                  }
                                                }
                                                if (actionName === 'Swipe') {
                                                  const start =
                                                    parsedAction.start;
                                                  const end = parsedAction.end;
                                                  if (
                                                    Array.isArray(start) &&
                                                    start.length === 2 &&
                                                    Array.isArray(end) &&
                                                    end.length === 2
                                                  ) {
                                                    const x1 =
                                                      (Math.max(
                                                        0,
                                                        Math.min(start[0], 1000)
                                                      ) /
                                                        1000) *
                                                      100;
                                                    const y1 =
                                                      (Math.max(
                                                        0,
                                                        Math.min(start[1], 1000)
                                                      ) /
                                                        1000) *
                                                      100;
                                                    const x2 =
                                                      (Math.max(
                                                        0,
                                                        Math.min(end[0], 1000)
                                                      ) /
                                                        1000) *
                                                      100;
                                                    const y2 =
                                                      (Math.max(
                                                        0,
                                                        Math.min(end[1], 1000)
                                                      ) /
                                                        1000) *
                                                      100;
                                                    return (
                                                      <svg className="absolute inset-0 w-full h-full pointer-events-none overflow-visible">
                                                        <defs>
                                                          <marker
                                                            id={`arrowhead-${idx}`}
                                                            markerWidth="6"
                                                            markerHeight="6"
                                                            refX="5"
                                                            refY="3"
                                                            orient="auto"
                                                          >
                                                            <polygon
                                                              points="0,0 6,3 0,6"
                                                              fill="rgba(239,68,68,0.9)"
                                                            />
                                                          </marker>
                                                        </defs>
                                                        <circle
                                                          cx={`${x1}%`}
                                                          cy={`${y1}%`}
                                                          r="4"
                                                          fill="rgba(239,68,68,0.9)"
                                                        />
                                                        <line
                                                          x1={`${x1}%`}
                                                          y1={`${y1}%`}
                                                          x2={`${x2}%`}
                                                          y2={`${y2}%`}
                                                          stroke="rgba(239,68,68,0.9)"
                                                          strokeWidth="3"
                                                          markerEnd={`url(#arrowhead-${idx})`}
                                                          strokeDasharray="5 3"
                                                        />
                                                      </svg>
                                                    );
                                                  }
                                                }
                                                return null;
                                              })()}
                                          </ImagePreview>
                                        </div>
                                      )}

                                      {stepAction && (
                                        <details className="mt-2 text-xs">
                                          <summary className="cursor-pointer text-[#1d9bf0] hover:text-[#1a8cd8] transition-colors">
                                            View action
                                          </summary>
                                          <pre className="mt-2 p-2 bg-slate-900 text-slate-200 rounded-lg overflow-x-auto text-xs border border-slate-800">
                                            {JSON.stringify(
                                              stepAction,
                                              null,
                                              2
                                            )}
                                          </pre>
                                        </details>
                                      )}
                                    </div>
                                  </>
                                );
                              })()}
                            </div>
                          );
                        })}

                        {message.observationWindows
                          ?.filter(
                            window =>
                              !message.stepNumbers?.includes(window.step)
                          )
                          .map(window => (
                            <ObservationWindowCard
                              key={`observation-${window.step}`}
                              window={window}
                            />
                          ))}

                        {/* Current thinking being streamed */}
                        {message.currentThinking && (
                          <div className="bg-slate-100 dark:bg-slate-800 rounded-2xl rounded-tl-sm px-4 py-3">
                            <div className="flex items-center gap-2 mb-2">
                              <div className="flex h-6 w-6 items-center justify-center rounded-full bg-[#1d9bf0]/10">
                                <Sparkles className="h-3 w-3 text-[#1d9bf0] animate-pulse" />
                              </div>
                              <span className="text-xs font-medium text-slate-500 dark:text-slate-400">
                                正在综合分析...
                              </span>
                            </div>
                            <p className="text-sm whitespace-pre-wrap text-slate-700 dark:text-slate-300">
                              {message.currentThinking}
                              <span className="inline-block w-1 h-4 ml-0.5 bg-[#1d9bf0] animate-pulse" />
                            </p>
                          </div>
                        )}

                        {/* Final result */}
                        {message.content &&
                          (() => {
                            const isTakeoverMsg =
                              message.content.startsWith(
                                'TAKEOVER_REQUIRED:'
                              ) ||
                              message.content.startsWith('INTERACT_REQUIRED:');
                            return (
                              <div
                                className={`rounded-2xl px-4 py-3 flex items-start gap-2 ${
                                  message.success === false
                                    ? 'bg-red-100 dark:bg-red-900/20 text-red-600 dark:text-red-400'
                                    : isTakeoverMsg
                                      ? 'bg-blue-50 dark:bg-blue-900/20 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800'
                                      : 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300'
                                }`}
                              >
                                {isTakeoverMsg ? (
                                  <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5 text-amber-500" />
                                ) : (
                                  <CheckCircle2
                                    className={`w-5 h-5 flex-shrink-0 mt-0.5 ${
                                      message.success === false
                                        ? 'text-red-500'
                                        : 'text-green-500'
                                    }`}
                                  />
                                )}
                                <div className="min-w-0 flex-1">
                                  <MarkdownContent content={message.content} />
                                  {message.steps !== undefined && (
                                    <p className="text-xs mt-2 opacity-60 text-slate-500 dark:text-slate-400">
                                      {message.steps} steps completed
                                    </p>
                                  )}
                                  {message.errorDetails && (
                                    <details className="mt-3 text-xs">
                                      <summary className="cursor-pointer font-medium text-red-700 hover:text-red-800 dark:text-red-300 dark:hover:text-red-200 transition-colors">
                                        Model error details
                                      </summary>
                                      <pre className="mt-2 max-h-80 overflow-auto rounded-lg border border-red-200 bg-white/80 p-3 text-left font-mono text-[11px] leading-relaxed text-slate-800 dark:border-red-900/60 dark:bg-slate-950/70 dark:text-slate-200">
                                        {formatModelErrorDetails(
                                          message.errorDetails
                                        )}
                                      </pre>
                                    </details>
                                  )}
                                </div>
                              </div>
                            );
                          })()}

                        {/* Streaming indicator */}
                        {message.isStreaming && (
                          <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                            <Loader2 className="w-4 h-4 animate-spin" />
                            Processing...
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="max-w-[75%]">
                        <div className="chat-bubble-user px-4 py-3 space-y-2">
                          {message.attachments &&
                            message.attachments.length > 0 && (
                              <div className="grid grid-cols-2 gap-2">
                                {message.attachments.map((attachment, idx) => (
                                  <ImagePreview
                                    key={`${message.id}-attachment-${idx}`}
                                    src={`data:${attachment.mime_type};base64,${attachment.data}`}
                                    alt={
                                      attachment.name || `Attachment ${idx + 1}`
                                    }
                                    className="w-full border-white/20"
                                    thumbnailClassName="w-full object-cover"
                                    maxHeight="96px"
                                  />
                                ))}
                              </div>
                            )}
                          {message.content && (
                            <MarkdownContent
                              content={message.content}
                              prose={false}
                            />
                          )}
                        </div>
                        <p className="text-xs text-slate-400 dark:text-slate-500 mt-1 text-right">
                          {message.timestamp.toLocaleTimeString()}
                        </p>
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </ScrollArea>
          {showNewMessageNotice && (
            <div className="pointer-events-none absolute inset-x-0 bottom-4 flex justify-center">
              <Button
                onClick={handleScrollToLatest}
                size="sm"
                className="pointer-events-auto shadow-lg bg-[#1d9bf0] text-white hover:bg-[#1a8cd8]"
                aria-label={t.devicePanel.newMessages}
              >
                {t.devicePanel.newMessages}
              </Button>
            </div>
          )}
        </div>

        {/* Input area */}
        <div
          className={`shrink-0 p-4 border-t border-slate-200 dark:border-slate-800 ${
            isDraggingAttachment
              ? 'bg-sky-50 dark:bg-sky-950/20'
              : 'bg-transparent'
          }`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            className="hidden"
            onChange={handleFileInputChange}
          />
          {waitingForDevice && (
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-300">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>Waiting for device...</span>
            </div>
          )}
          {waitingForUserInteraction && (
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-700 dark:border-blue-900/50 dark:bg-blue-950/30 dark:text-blue-300">
              <Hand className="h-4 w-4" />
              <span className="whitespace-pre-line">
                {interactionPrompt || '等待用户输入...'}
              </span>
            </div>
          )}
          {attachments.length > 0 && (
            <div className="mb-3 flex flex-wrap gap-2">
              {attachments.map((attachment, idx) => (
                <div
                  key={`${attachment.name || 'image'}-${idx}`}
                  className="relative"
                >
                  <ImagePreview
                    src={`data:${attachment.mime_type};base64,${attachment.data}`}
                    alt={attachment.name || `Attachment ${idx + 1}`}
                    className="h-16 w-16 border-slate-200 dark:border-slate-700 bg-slate-100 dark:bg-slate-800"
                    thumbnailClassName="h-full w-full object-cover"
                    maxHeight="64px"
                  />
                  <button
                    type="button"
                    onClick={() => removeAttachment(idx)}
                    className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full bg-slate-950/70 text-white hover:bg-slate-950 z-10"
                    aria-label="移除图片"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex items-end gap-3">
            <Textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleInputKeyDown}
              onPaste={handlePaste}
              placeholder={
                waitingForUserInteraction
                  ? interactionPrompt || '请输入您的回复'
                  : !isConfigured
                    ? t.devicePanel.configureFirst
                    : t.devicePanel.whatToDo
              }
              disabled={loading && !waitingForUserInteraction}
              className="flex-1 min-h-[40px] max-h-[120px] resize-none"
              rows={1}
            />
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  disabled={
                    loading || attachments.length >= MAX_IMAGE_ATTACHMENTS
                  }
                  className="h-10 w-10 flex-shrink-0"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <ImagePlus className="w-4 h-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top" sideOffset={8}>
                添加图片
              </TooltipContent>
            </Tooltip>
            {/* Workflow Quick Run Button */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Popover
                  open={showWorkflowPopover}
                  onOpenChange={setShowWorkflowPopover}
                >
                  <PopoverTrigger asChild>
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-10 w-10 flex-shrink-0"
                    >
                      <ListChecks className="w-4 h-4" />
                    </Button>
                  </PopoverTrigger>
                  <PopoverContent align="start" className="w-72 p-3">
                    <div className="space-y-2">
                      <h4 className="font-medium text-sm">
                        {t.workflows.selectWorkflow}
                      </h4>
                      {workflows.length === 0 ? (
                        <div className="text-sm text-slate-500 dark:text-slate-400 space-y-1">
                          <p>{t.workflows.empty}</p>
                          <p>
                            前往{' '}
                            <a
                              href="/workflows"
                              className="text-primary underline"
                            >
                              工作流
                            </a>{' '}
                            页面创建。
                          </p>
                        </div>
                      ) : (
                        <ScrollArea className="h-64">
                          <div className="space-y-1">
                            {workflows.map(workflow => (
                              <button
                                key={workflow.uuid}
                                onClick={() => handleExecuteWorkflow(workflow)}
                                className="w-full text-left p-2 rounded hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                              >
                                <div className="font-medium text-sm">
                                  {workflow.name}
                                </div>
                                <div className="text-xs text-slate-500 dark:text-slate-400 line-clamp-2">
                                  {workflow.text}
                                </div>
                              </button>
                            ))}
                          </div>
                        </ScrollArea>
                      )}
                    </div>
                  </PopoverContent>
                </Popover>
              </TooltipTrigger>
              <TooltipContent side="top" sideOffset={8} className="max-w-xs">
                <div className="space-y-1">
                  <p className="font-medium">
                    {t.devicePanel.tooltips.workflowButton}
                  </p>
                  <p className="text-xs opacity-80">
                    {t.devicePanel.tooltips.workflowButtonDesc}
                  </p>
                </div>
              </TooltipContent>
            </Tooltip>
            {/* Abort Button - shown when loading */}
            {loading && (
              <Button
                onClick={handleAbortChat}
                disabled={aborting}
                size="icon"
                variant="destructive"
                className="h-10 w-10 rounded-full flex-shrink-0"
                title={t.chat.abortChat}
              >
                {aborting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Square className="h-4 w-4" />
                )}
              </Button>
            )}
            {/* Send Button */}
            {!loading && (
              <Button
                onClick={handleSend}
                disabled={
                  (!input.trim() && attachments.length === 0) || !sessionReady
                }
                size="icon"
                variant="twitter"
                className="h-10 w-10 rounded-full flex-shrink-0"
                title={
                  interactionMode === 'experience' ? '生成体验草案' : '发送'
                }
              >
                <Send className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      </Card>

      <DeviceMonitor
        deviceId={deviceId}
        serial={deviceSerial}
        connectionType={deviceConnectionType}
        isVisible={isVisible} // ✅ 修改：传递实际的 isVisible（原为硬编码 true）
      />
    </div>
  );
}
