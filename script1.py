athlete = {
    "name": "Isai",
    "sleep": 8
}

def athlete_time(sleep):
    if athlete["sleep"]>= 8:
        return 'athlete well rested'
    else:
        return 'athlete needs more sleep'

print('Isai is a '+ athlete(8))