-- Library > Plug-in Extras > "Sync Post Collections": one click, full sync,
-- with a visible result (dialog on problems, bezel on success).

local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'

LrTasks.startAsyncTask(function()
    local ok, Sync = pcall(require, 'Sync')
    if not ok then
        LrDialogs.message('Posts Sync', 'Plugin failed to load: ' .. tostring(Sync))
        return
    end
    local ran, res = pcall(Sync.sync)
    if not ran then
        LrDialogs.message('Posts Sync', 'Sync failed: ' .. tostring(res))
    elseif res.err then
        LrDialogs.message('Posts Sync', res.err)
    elseif #res.bad > 0 then
        LrDialogs.message('Posts Sync', string.format(
            '%d collection(s) synced into "%s".\nSkipped: %s',
            res.synced, Sync.SET_NAME, table.concat(res.bad, ', ')))
    else
        LrDialogs.showBezel(string.format(
            'Posts Sync: %d collection(s) synced into "%s"', res.synced, Sync.SET_NAME))
    end
end)
