<script setup>
import { onMounted, onUnmounted, ref } from 'vue'

const dialog = ref(null)
const src = ref('')
const alt = ref('')
let previousFocus = null

function onClick(event) {
  const img = event.target instanceof Element ? event.target.closest('.vp-doc img') : null
  if (!img || !dialog.value) return
  src.value = img.currentSrc || img.src
  alt.value = img.alt || ''
  previousFocus = document.activeElement
  dialog.value.showModal()
}

function onClose() {
  if (previousFocus instanceof HTMLElement) previousFocus.focus()
}

function close() {
  dialog.value?.close()
}

onMounted(() => document.addEventListener('click', onClick))
onUnmounted(() => document.removeEventListener('click', onClick))
</script>

<template>
  <dialog ref="dialog" class="diagram-zoom" :aria-label="alt || 'Diagram preview'" @click="close" @close="onClose">
    <img v-if="src" :src="src" :alt="alt" />
  </dialog>
</template>
