athelte =  {'Name':'Diego','Age':22,'event':'5k','pr':'14:55','mileage':55}

athelte['Age'] = 23

athelte['Coach'] ='Mike'

print(athelte)

print(athelte.keys())
print(athelte.values())

del athelte['mileage']

athelte['Country'] = 'USA'

athelte['event'] = 1500

print(athelte['Country'])

for value in athelte.values():
    print(value)

for key, value in athelte.items():
    print(key, value)

if athelte['Country'] == 'USA':
    print('Country is USA')

