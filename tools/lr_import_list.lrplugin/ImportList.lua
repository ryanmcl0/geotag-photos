-- "Import photos from list file…": pick any list file, then import it.

local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'

LrTasks.startAsyncTask(function()
    local ok, Core = pcall(require, 'ImportCore')
    if not ok then
        LrDialogs.message('Import from List', 'Plugin failed to load: ' .. tostring(Core))
        return
    end
    local listPath = Core.chooseList()
    if listPath then Core.importList(listPath) end
end)
