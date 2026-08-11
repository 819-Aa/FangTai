<script setup lang="ts">
import { UserRound, X } from "@lucide/vue";
import type { AnonymousParticipant } from "@/types";

const props = defineProps<{
  slots: AnonymousParticipant[];
  selectedRefs: string[];
  disabled: boolean;
}>();
const emit = defineEmits<{
  add: [];
  remove: [ref: string];
}>();
</script>

<template>
  <div class="participant-context">
    <div class="chip-list">
      <span v-for="s in slots" :key="s.participant_ref" class="chip">
        <UserRound :size="13" />
        <span>{{ s.label }}</span>
        <i class="chip-note">匿名</i>
        <button
          type="button"
          class="chip-x"
          :disabled="disabled"
          @click="emit('remove', s.participant_ref)"
        >
          <X :size="12" />
        </button>
      </span>
    </div>

    <button type="button" class="add-slot" :disabled="disabled" @click="emit('add')">
      <UserRound :size="15" />
      <span>添加匿名成员</span>
    </button>
  </div>
</template>
