--[[ "Import Photos of Me": one click, never a file picker. Imports the one
     photos-of-me list (LIST_PATH) that face_scan_review.py keeps appending
     to. If the file is not there (drive not mounted), it says so and stops. ]]

local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrTasks = import 'LrTasks'

local LIST_PATH = '/Volumes/RYAN/photos_of_me.txt'

LrTasks.startAsyncTask(function()
    local ok, Core = pcall(require, 'ImportCore')
    if not ok then
        LrDialogs.message('Import Photos of Me', 'Plugin failed to load: ' .. tostring(Core))
        return
    end
    if not LrFileUtils.exists(LIST_PATH) then
        LrDialogs.message('Import Photos of Me',
            'List file not found:\n' .. LIST_PATH .. '\n\nIs the drive mounted?')
        return
    end
    Core.importList(LIST_PATH)
end)
