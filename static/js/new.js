/**
 * Perform device maintenance action
 */
export async function doAction(action, ieee) {
    let shouldBan = false;

    // Special handling for 'remove' to ask about banning
    if (action === 'remove') {
        if (!confirm("Are you sure you want to remove this device?")) return;

        // Secondary prompt: Ask if the user wants to ban
        shouldBan = confirm("Do you also want to BAN this device to prevent it from rejoining?\n\nClick OK to Remove & Ban.\nClick Cancel to just Remove.");
    } else {
        // For other actions (like restart/interview), keep the generic confirm if needed
    }

    try {
        const res = await fetch(`/api/device/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // Pass the ban flag (defaults to false if not set above)
            body: JSON.stringify({
                ieee: ieee,
                force: false,
                ban: shouldBan
            })
        });
        const data = await res.json();

        if (data.success) {
            let logMsg = `${action.toUpperCase()} sent.`;
            if (action === 'remove' && shouldBan) {
                logMsg = "Device removed and BANNED.";
            } else if (action === 'remove') {
                logMsg = "Device removed.";
            }

            addLogEntry({
                timestamp: getTimestamp(),
                level: 'INFO',
                message: logMsg
            });

            // Optional: Refresh the list or UI if needed
            if (action === 'remove') {
                alert(logMsg);
                // If you have a refresh function exposed:
                // if (window.refreshDevices) window.refreshDevices();
            }
        } else {
            alert(`Error: ${data.error}`);
        }
    } catch (e) {
        console.error(e);
        alert("Action failed: " + e.message);
    }
}