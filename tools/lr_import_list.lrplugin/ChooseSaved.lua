-- "Choose Photos of Me list…": change the file "Import Photos of Me" uses.

local LrDialogs = import 'LrDialogs'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'

LrTasks.startAsyncTask(function()
    local ok, Core = pcall(require, 'ImportCore')
    if not ok then
        LrDialogs.message('Import Photos of Me', 'Plugin failed to load: ' .. tostring(Core))
        return
    end
    local prefs = LrPrefs.prefsForPlugin()
    local listPath = Core.chooseList('Choose the photos-of-me list file')
    if listPath then
        prefs.savedListPath = listPath
        LrDialogs.showBezel('Import Photos of Me will use ' .. listPath)
    end
end)
