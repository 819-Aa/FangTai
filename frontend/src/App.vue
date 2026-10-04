<script setup lang="ts">
import { Sparkles } from "@lucide/vue";

import ChatPanel from "@/features/chat/ChatPanel.vue";
import ChatSidebar from "@/features/chat/ChatSidebar.vue";
import ParticipantContext from "@/features/participants/ParticipantContext.vue";
import { useRecommendationStore } from "@/stores/recommendation";

const store = useRecommendationStore();
</script>

<template>
  <div class="app-frame">
    <ChatSidebar
      :chats="store.chats"
      :active-chat-id="store.activeChatId"
      :disabled="store.isStreaming || store.isRecovering"
      :storage-error="store.storageError"
      @new-chat="store.newChat"
      @open-chat="store.openChat"
    />
    <div class="app-main">
      <header class="app-header">
        <div class="brand">
          <span class="brand-icon"><Sparkles :size="18" /></span>
          <span>健康菜品推荐系统 V2</span>
        </div>
        <div class="header-actions">
          <ParticipantContext
            :slots="store.slots"
            :selected-refs="store.selectedRefs"
            :disabled="!store.canEditParticipants"
            @toggle="store.toggleSlot"
          />
        </div>
      </header>

      <div v-if="store.error || store.status === 'recovery_required'" class="global-error" role="alert">
        {{ store.error || '请求暂未完成，可以恢复生成。' }}
        <button v-if="store.pendingRequest && store.status === 'recovery_required'"
          type="button" class="recovery-button" :disabled="!store.canRecover" @click="store.recoverRequest">
          {{ store.isRecovering ? '正在恢复…' : '恢复生成' }}
        </button>
      </div>

      <ChatPanel
        class="chat-host"
        :messages="store.messages"
        :phases="store.phases"
        :answer="store.answer"
        :clarification="store.clarification"
        :active-clarification="store.activeClarification"
        :status="store.status"
        :is-streaming="store.isStreaming"
        :is-cancelling="store.isCancelling"
        :can-send="store.canSend"
        :participants="store.selectedSlots"
        :current-menu="store.currentMenu"
        :pending-replace-dish="store.pendingReplaceDish"
        @send="store.send"
        @select-option="store.selectOption"
        @cancel="store.cancel"
        @replace-dish="store.startReplaceDish"
        @cancel-replace-dish="store.cancelReplaceDish"
      />
    </div>
  </div>
</template>
