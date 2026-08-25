-- Startup sync: shortly after Lightroom launches, run one silent sync so the
-- Posts collection set is current without any clicks. If the network drive
-- is not mounted yet, retry once a minute for ~10 minutes, then give up
-- until the next launch (the menu item still works any time).

local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'

LrTasks.startAsyncTask(function()
    local ok, Sync = pcall(require, 'Sync')
    if not ok then return end
    LrTasks.sleep(20)   -- let the catalog finish waking up
    for attempt = 1, 10 do
        local ran, res = pcall(Sync.sync)
        if ran and not res.err then
            if res.synced > 0 then
                LrDialogs.showBezel(string.format(
                    'Posts Sync: %d collection(s) synced', res.synced))
            end
            return
        end
        LrTasks.sleep(60)
    end
end)
