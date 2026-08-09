Athlete = { "Diego":
{"5k":"15:15","Mileage":50,"Coach":"Torres"}}

for value in Athlete:
    print(value, "is a Naia national qualfier!")


if Athlete["Diego"]["Coach"] == "Torres":
    print("Coach is Torres")



else:
    print("Coach not found")
print(Athlete["Diego"]["5k"])
print("Diego's race goal is 15:05")

if Athlete["Diego"]["5k"] <= "15:05":
    print("Diego's race goal is achieved")
else:
    print("Diego's race goal is not achieved")



