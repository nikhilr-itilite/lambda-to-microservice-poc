# mypy: ignore-errors
from geopy.distance import geodesic


def generate_hexagonal_grid(lat, long, large_radius_km, small_radius_km):
    """
    Generate a list of latitude and longitude coordinates arranged in a hexagonal grid
    around a central point.

    Parameters:
    - lat: Central latitude
    - long: Central longitude
    - large_radius_km: Radius of the large circle (in kilometers)
    - small_radius_km: Radius of each smaller circle (in kilometers)

    Returns:
    - List of [latitude, longitude] pairs
    """
    # Convert radius from kilometers to degrees (approximate)
    small_radius_deg = small_radius_km / 111
    row_height = small_radius_deg * 1.5
    col_width = small_radius_deg * (3**0.5)

    # num_rows = int((large_radius_km / small_radius_km) * 1.5) * 2 + 1
    # num_cols = int((large_radius_km / small_radius_km) * (3 ** 0.5)) * 2 + 1

    # Estimate the number of rows and columns needed
    num_rows = int((large_radius_km * 2 / (small_radius_km * 1.5))) + 2
    num_cols = int((large_radius_km * 2 / (small_radius_km * (3**0.5)))) + 2

    all_lat_long = []

    for row in range(num_rows):
        for col in range(num_cols):
            offset_lat = lat + (row - num_rows // 2) * row_height
            offset_long = long + (col - num_cols // 2) * col_width

            if row % 2 == 1:
                offset_long += col_width / 2

            # Check if the offset point is within the large circle radius
            # if geodesic((lat, long), (offset_lat, offset_long)).km <= large_radius_km + small_radius_km:
            if geodesic((lat, long), (offset_lat, offset_long)).km <= large_radius_km:
                all_lat_long.append([offset_lat, offset_long])

    return all_lat_long


def plot_coordinates(lat, lng, large_radius_km, grid_lat_long):
    """
    Plot the central circle and the hexagonal grid of smaller circles on a map.

    This can be used to visualise how every vendor call is being made.
    Parameters:
    - lat: Central latitude
    - lng: Central longitude
    - large_radius_km: Radius of the large circle (in kilometers)
    - grid_lat_long: List of [latitude, longitude, radius] pairs for the hexagonal grid
    """
    import matplotlib.pyplot as plt  # Importing here as this will be utilised only for testing locally.

    # Convert radius from kilometers to degrees (approximate)
    def radius_in_degree(rad):
        return rad / 111

    def plot_circle(ax, lat, lng, radius, color="blue", alpha=0.5):
        circle = plt.Circle((lng, lat), radius_in_degree(radius), color=color, fill=True, alpha=alpha)
        ax.add_patch(circle)

    fig, ax = plt.subplots()
    ax.set_aspect("equal")

    # Plot the original circle
    plot_circle(ax, lat, long, large_radius_km, color="red", alpha=0.3)
    ax.plot(long, lat, "ro")  # Mark the original point

    # Plot the grid circles
    for offset_lat, offset_long, radius in grid_lat_long:
        plot_circle(ax, offset_lat, offset_long, radius, color="blue", alpha=0.3)
        ax.plot(offset_long, offset_lat, "bo")  # Mark the offset points

    # canvas_width = 1.5 * radius_in_degree(large_radius_km)
    # ax.set_xlim(long - canvas_width, long + canvas_width)
    # ax.set_ylim(lat - canvas_width, lat + canvas_width)
    plt.xlabel("Longitude")
    plt.ylabel("Latitude")
    plt.title("Radius with Extended Hexagonal Grid Offset Calculations")

    # Save and show the plot
    plt.savefig("extended_hexagonal_grid_radius_visualization.png")

    # plt.show()


def get_equivalent_smaller_grids(latitude, longitude, radius, depth_degree=1):
    """
    Generates a list of smaller hexagonal grids within a given radius around a central location
    by recursively dividing the grids into smaller ones.

    Parameters:
    - latitude (float): The latitude of the central point in decimal degrees.
    - longitude (float): The longitude of the central point in decimal degrees.
    - radius (float): The initial radius in kilometers within which to generate smaller grids.
    - depth_degree (int): The number of recursive levels to divide the grid. Default is 1.

    Returns:
    - list of tuples: A list of locations in the form of (latitude, longitude, radius),
      where each location represents a smaller grid location at each division level.
    """

    GRID_DIVISION_FACTOR = 2  # Factor by which to divide the radius to create smaller grids
    smaller_radius = radius
    locations = [(latitude, longitude, radius)]  # List to hold the central location and smaller grid locations

    while depth_degree > 0:
        depth_degree -= 1
        central_grid_location = (latitude, longitude, smaller_radius)
        locations.remove(central_grid_location)  # Remove the central location from the list of locations
        # Generate the smaller grid based on the new radius
        inner_hex_grid = generate_hexagonal_grid(latitude, longitude, smaller_radius, smaller_radius / GRID_DIVISION_FACTOR)
        smaller_radius /= GRID_DIVISION_FACTOR  # Reduce the radius for the next level of grids
        # Add the new smaller grid locations to the list
        locations.extend([(lat, lng, smaller_radius) for lat, lng in inner_hex_grid])

    return locations


if __name__ == "__main__":
    # Example coordinates and radius
    lt, long, large_radius_km = 36.171563, -115.1391009, 30
    depth_degree = 3
    # # Generate the grid coordinates
    grid_lat_long = get_equivalent_smaller_grids(lt, long, large_radius_km, depth_degree)
    print(len(grid_lat_long), ": ", grid_lat_long)
    # # Plot the coordinates
    plot_coordinates(lt, long, large_radius_km, grid_lat_long)
