--[[ "Import Photos of Me": one click, no file picker. Imports the list file
     saved in the plugin's preferences. The first run (or when the saved file
     is gone, e.g. the drive is not mounted) asks for the file once and
     remembers it. "Choose Photos of Me list…" changes it. ]]

local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'

LrTasks.startAsyncTask(function()
    local ok, Core = pcall(require, 'ImportCore')
    if not ok then
        LrDialogs.message('Import Photos of Me', 'Plugin failed to load: ' .. tostring(Core))
        return
    end
    local prefs = LrPrefs.prefsForPlugin()
    local listPath = prefs.savedListPath
    if not listPath or not LrFileUtils.exists(listPath) then
        if listPath then
            LrDialogs.message('Import Photos of Me',
                'The saved list file was not found:\n' .. listPath ..
                '\n\nIf its drive is not mounted, mount it and try again. ' ..
                'Otherwise choose the file in the next step.')
        end
        listPath = Core.chooseList('Choose the photos-of-me list file (asked once, then remembered)')
        if not listPath then return end
        prefs.savedListPath = listPath
    end
    Core.importList(listPath)
end)
