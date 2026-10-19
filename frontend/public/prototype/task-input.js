(() => {
  // One sender owns the complete attachment-upload + input-submit operation for a task.
  // A failed input stays first until an explicit retry reuses that same input identity.
  const createSender = (onFailure) => {
    const inputs = [];
    let send = null;
    let active = false;
    let blocked = false;
    const drain = async () => {
      if (!send || active || blocked) return;
      active = true;
      try {
        while (inputs.length) {
          try {
            await send(inputs[0]);
            inputs.shift();
          } catch (error) {
            blocked = true;
            onFailure(inputs[0], error);
            break;
          }
        }
      } finally {
        active = false;
      }
    };
    return {
      get isSending() { return active; },
      connect(handler) { send = handler; void drain(); },
      submit(input) { inputs.push(input); void drain(); },
      retry() { blocked = false; void drain(); },
      dispose() { send = null; blocked = true; inputs.length = 0; },
    };
  };
  window.CAD2BIM_TASK_INPUT = { createSender };
})();
